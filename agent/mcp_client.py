"""Minimal MCP JSON-RPC client (user token) for the Genie Agent MCP and the Gateway dbsql MCP. Polling stays in code."""
import json
import time

import requests

import os

MAX_WAIT_S = 90
MAX_ROWS = 25
# adaptive polling: first wait, then x2 up to the cap (seconds)
POLL_FIRST_S = float(os.environ.get("BEV_POLL_FIRST_S", "0.5"))
POLL_MAX_S = float(os.environ.get("BEV_POLL_MAX_S", "2"))


def _waits():
    w = POLL_FIRST_S
    while True:
        yield w
        w = min(w * 2, POLL_MAX_S)


def _rpc(ctx, path, name, args, timeout=120):
    from agent.gateway_tags import header
    hdr = ctx.headers() | {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"} | header(f"mcp:{name.split('_')[0]}")
    r = requests.post(f"{ctx.wc.config.host}{path}", headers=hdr, timeout=timeout,
                      json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    r.raise_for_status()
    txt = r.text
    if not txt.lstrip().startswith("{"):
        txt = next(l[5:].strip() for l in txt.splitlines() if l.startswith("data:"))
    body = json.loads(txt)
    if "error" in body:
        raise RuntimeError(body["error"].get("message", "MCP error"))
    res = body["result"]
    text = "".join(c.get("text", "") for c in res.get("content", []))
    if res.get("isError"):
        raise RuntimeError(text[:500])
    return text


def _trim_statement(sr):
    cols = [c["name"] for c in sr.get("manifest", {}).get("schema", {}).get("columns", [])]
    res = sr.get("result") or {}
    rows = res.get("data_array")
    if rows is None:
        # dbsql MCP returns typed values: [{"values": [{"str": ..}|{"null_value": ..}]}]
        rows = [[next(iter(v.values())) if v else None for v in r.get("values", [])] for r in res.get("data_typed_array") or []]
        rows = [[None if isinstance(x, dict) else x for x in r] for r in rows]
    return {"columns": cols, "rows": rows[:MAX_ROWS], "row_count": len(rows), "truncated": len(rows) > MAX_ROWS}


def genie_ask(ctx, space_id, question):
    # ask, then poll in code (the LLM never polls); returns compact SQL + rows + text
    path = f"/api/2.0/mcp/genie/{space_id}"
    d = json.loads(_rpc(ctx, path, f"query_space_{space_id}", {"query": question}))
    t0, waits = time.time(), _waits()
    while d.get("status") not in ("COMPLETED", "FAILED", "CANCELLED") and time.time() - t0 < MAX_WAIT_S:
        time.sleep(next(waits))
        d = json.loads(_rpc(ctx, path, f"poll_response_{space_id}",
                            {"conversation_id": d["conversationId"], "message_id": d["messageId"]}))
    content = d.get("content") or {}
    out = {"status": d.get("status"), "conversation_id": d.get("conversationId"), "text": [], "queries": []}
    for t in content.get("textAttachments") or []:
        out["text"].append(t if isinstance(t, str) else t.get("content", ""))
    for q in content.get("queryAttachments") or []:
        out["queries"].append({"sql": q.get("query"), "description": q.get("description"), **_trim_statement(q.get("statement_response") or {})})
    if out["status"] != "COMPLETED":
        out["error"] = "Genie did not complete in time" if time.time() - t0 >= MAX_WAIT_S else f"Genie status {out['status']}"
    return out


def sql_call(ctx, statement):
    # statement is built in code from validated arguments (never by the LLM)
    d = json.loads(_rpc(ctx, ctx_dbsql(), "execute_sql", {"query": statement}))
    t0, waits = time.time(), _waits()
    while _state(d) in ("PENDING", "RUNNING") and time.time() - t0 < MAX_WAIT_S:
        time.sleep(next(waits))
        d = json.loads(_rpc(ctx, ctx_dbsql(), "poll_sql_result", {"statement_id": d["statement_id"]}))
    if _state(d) != "SUCCEEDED":
        raise RuntimeError(f"SQL {_state(d)}")
    return _trim_statement(d)


def _state(d):
    return (d.get("status") or {}).get("state")


def ctx_dbsql():
    from agent.context import DBSQL_MCP
    return DBSQL_MCP
