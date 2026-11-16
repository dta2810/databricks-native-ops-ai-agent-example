"""GraphBridge: runs the LangGraph agent for the app (BEV_AGENT_MODE=graph) and maps graph events to the UI stream contract (app/API.md).
Checkpoints in Lakebase (schema BEV_AGENT_STATE_SCHEMA) keyed by the namespaced thread id, so conversations resume across restarts."""
import json
import logging
import os
import re
from contextlib import contextmanager

import mlflow
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command

from agent.context import CATALOG, SCHEMA, RequestContext
from agent.graph import TRUNCATED_NOTE, build_graph
from agent.messages import text_of
from agent.tracing import init_tracing, trace_user, turn_span
from memory.pgretry import is_transient, retry_once

logger = logging.getLogger(__name__)
_SCRUB = [(re.compile(r"(eyJ[A-Za-z0-9_-]{10})[A-Za-z0-9_.-]+"), r"\1<redacted>"),
          (re.compile(r"\b[\w.-]+\.(?:azure)?databricks\.(?:net|com)\b", re.I), "<host>"),
          (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
          (re.compile(r"\bpostgresql://\S+"), "<connection>")]


def _scrub(e, limit=300):
    msg = f"{type(e).__name__}: {e}"
    for pat, repl in _SCRUB:
        msg = pat.sub(repl, msg)
    msg = " ".join(msg.split())
    return msg[:limit]

KIND = {"ask_commercial": "genie", "ask_supply": "genie", "search_documents": "documents", "recall_decisions": "memory",
        "propose_replenishment": "request", "my_open_requests": "request", "amend_request": "request", "withdraw_request": "request",
        "requests_summary": "request"}
LABEL = {
    "ask_commercial": "Asking the Commercial & Promotions Genie agent", "ask_supply": "Asking the Supply & Replenishment Genie agent",
    "stock_cover": "Checking stock cover", "oos_risk_skus": "Checking out-of-stock risk", "suggest_replenishment": "Sizing the replenishment",
    "promo_performance": "Checking promotions", "promo_by_distributor": "Measuring the promotion by distributor",
    "search_documents": "Searching documents", "recall_decisions": "Checking past decisions", "propose_replenishment": "Preparing the request",
    "distributor_snapshot": "Checking the distributor's health", "my_open_requests": "Listing your open requests",
    "amend_request": "Changing the request", "withdraw_request": "Withdrawing the request", "requests_summary": "Counting requests by status",
}
MAX_ROWS_UI = int(os.environ.get("BEV_UI_MAX_ROWS", "10"))
RECURSION_LIMIT = int(os.environ.get("BEV_RECURSION_LIMIT", "25"))
# a turn that produces nothing for this long is abandoned: the UI gets an error and the trace is closed as ERROR
TURN_TIMEOUT_S = float(os.environ.get("BEV_TURN_TIMEOUT_S", "240"))


def _checkpointer():
    from databricks.sdk import WorkspaceClient
    from databricks_langchain import CheckpointSaver
    from psycopg_pool import ConnectionPool
    prof = os.environ.get("DATABRICKS_CONFIG_PROFILE")
    wc = WorkspaceClient(profile=prof) if prof and not os.environ.get("DATABRICKS_APP_NAME") else WorkspaceClient()
    saver = CheckpointSaver(project=os.environ.get("BEV_LAKEBASE_PROJECT"), branch=os.environ.get("BEV_LAKEBASE_BRANCH", "production"),
                            schema=os.environ.get("BEV_AGENT_STATE_SCHEMA", "agent_state"), workspace_client=wc,
                            # Lakebase can drop connections (suspend/restart -> AdminShutdown): validate on checkout, recycle
                            check=ConnectionPool.check_connection, max_lifetime=300, max_idle=300)
    return saver


def _rows(out):
    cols, rows = out.get("columns") or [], out.get("rows") or []
    return [dict(zip(cols, r)) for r in rows[:MAX_ROWS_UI]]


def _tool_end(msg, args):
    name, ok = msg.name, not str(msg.content).startswith("Tool error")
    ev = {"type": "tool_end", "id": msg.tool_call_id, "ok": ok, "summary": "", "sources": []}
    if not ok:
        ev["summary"] = "The tool returned an error"
        return ev
    try:
        out = json.loads(msg.content)
    except (TypeError, ValueError):
        return ev | {"summary": "Done"}
    if KIND.get(name) == "genie":
        qs = out.get("queries") or []
        ev["sources"] = [{"kind": "sql", "title": f"Genie ({name.split('_', 1)[1]})", "sql": q.get("sql")} for q in qs if q.get("sql")]
        ev["rows"] = _rows(qs[0]) if qs else []
        ev["summary"] = f"{qs[0].get('row_count', 0)} rows" if qs else (out.get("error") or "Answered")
    elif name == "search_documents":
        ev["sources"] = [{"kind": "doc", "doc_id": d["doc_id"], "section": d.get("section"),
                          "snippet": (d.get("content") or "").split("\n", 1)[-1][:280]} for d in out]
        ev["summary"] = f"{len(out)} passages"
    elif name == "recall_decisions":
        ds = out.get("decisions", []) if isinstance(out, dict) else out
        ev["sources"] = [{"kind": "memory", "decision_id": str(d.get("decision_id")), "created_at": str(d.get("created_at"))[:10],
                          "summary": d.get("summary")} for d in ds]
        ev["summary"] = f"{len(ds)} earlier decision{'s' if len(ds) != 1 else ''}"
    elif name == "requests_summary":
        t = out.get("totals") or {}
        ev["summary"] = ", ".join(f"{v['requests']} {k}" for k, v in t.items()) or "No requests"
    elif name == "propose_replenishment":
        ev["summary"] = "Request prepared, waiting for approval"
    elif name == "my_open_requests":
        ev["summary"] = f"{len(out)} open request{'s' if len(out) != 1 else ''}"
    elif name in ("amend_request", "withdraw_request"):
        ev["summary"] = "Request amended" if name == "amend_request" else "Request withdrawn"
    else:
        arglist = ", ".join(repr(v) for v in (args or {}).values())
        ev["sources"] = [{"kind": "sql", "title": f"UC function {name}", "sql": f"SELECT * FROM {CATALOG}.{SCHEMA}.{name}({arglist})"}]
        ev["rows"] = _rows(out)
        ev["summary"] = f"{out.get('row_count', 0)} rows"
    return ev


def _card_intro(req, left, resumed=False, after_text=False):
    # one line before each card, so a proposal turn is never blank and a queue shows its position
    what = f"{_fmt_qty(req.get('qty_cases'))} cases of {req.get('product_name') or req.get('sku')} for {req.get('distributor_id')}"
    more = f" ({left} more after this one)" if left else ""
    if resumed:
        return f"Next request{more}: {what}."
    return ("\n\n" if after_text else "") + f"Request ready for your decision{more}: {what}."


def _fmt_qty(v):
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _enrich(req, messages):
    # cover/target from the latest sizing for the same position, for the approval card
    req = dict(req)
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name == "suggest_replenishment":
            try:
                for r in _rows(json.loads(m.content)):
                    if r.get("distributor_id") == req.get("distributor_id") and r.get("sku") == req.get("sku"):
                        # needs_manager_approval comes from the proposed quantity (propose_request), never from the sizing row
                        req.update({k: r.get(k) for k in ("cover_days", "target_cover_days")})
                        return req
            except (TypeError, ValueError):
                pass
    return req


def _jsonable(d):
    # Decimal (Postgres numeric) -> float so the UI gets numbers, everything else -> str
    from decimal import Decimal
    return json.loads(json.dumps(d, default=lambda v: float(v) if isinstance(v, Decimal) else str(v)))


class GraphBridge:
    mode = "graph"

    def __init__(self):
        init_tracing()
        self._saver_obj = None
        self.inflight = {}  # turn key -> start time (monotonic), for /api/health

    @property
    def _saver(self):
        # opened once per process: FastAPI lifespan calls open(); scripts/eval open lazily on first use
        if self._saver_obj is None:
            self._saver_obj = _checkpointer()
        return self._saver_obj

    def open(self):
        # warm per-process caches at startup: prompt templates from the registry (~3-4 s cold)
        from agent.prompts import router_prompt, system_prompt
        system_prompt()
        router_prompt("warm-up")
        return self._saver

    def feedback(self, trace_id, value, comment, user_email, assessment_id=None, attempts=4):
        # human assessment on the turn's trace (runs as the app SP, like tracing); a re-rate overrides the previous one.
        # traces export asynchronously: retry briefly when the trace is not there yet. Returns the assessment id.
        import time
        from mlflow.entities import AssessmentSource, AssessmentSourceType
        src = AssessmentSource(source_type=AssessmentSourceType.HUMAN, source_id=trace_user(user_email))  # masked like the trace user
        for i in range(attempts):
            try:
                if assessment_id:
                    a = mlflow.override_feedback(trace_id=trace_id, assessment_id=assessment_id, value=value,
                                                 rationale=comment or None, source=src)
                else:
                    a = mlflow.log_feedback(trace_id=trace_id, name="user_feedback", value=value, rationale=comment or None, source=src)
                return a.assessment_id
            except Exception as e:
                if i == attempts - 1 or "not found" not in str(e).lower():
                    raise
                time.sleep(2 ** i)

    def close(self):
        try:
            from server.auth import close_pools
            close_pools()
        except ImportError:  # scripts/eval: no app server package
            pass
        if self._saver_obj is not None:
            self._saver_obj.__exit__(None, None, None)
            self._saver_obj = None

    def _ctx(self, user):
        from server import settings as S

        @contextmanager
        def pg(role):
            group = S.APPROVER_GROUP if role == "approver" else (user.read_role or S.PLANNER_GROUP)
            with user.connection(group) as conn:
                yield conn

        @contextmanager
        def service():
            with user.service() as conn:
                yield conn

        return RequestContext(wc=user.client(), user_email=user.email, pg=pg, service=service, roles=list(user.roles),
                              can_propose=user.can_propose, manager_ok=user.is_manager,
                              allow_self_approval=S.ALLOW_SELF_APPROVAL,
                              can_decide_own=bool(S.ALLOW_SELF_APPROVAL and user.can_approve))

    def _graph(self, user=None):
        return build_graph(self._ctx(user) if user else _NoCtx(), checkpointer=self._saver)

    def pending_approval(self, thread_id):
        st = retry_once(lambda: self._graph().get_state({"configurable": {"thread_id": thread_id}}))
        if st and st.interrupts:
            return _jsonable(_enrich(st.interrupts[0].value["request"], st.values.get("messages", [])))
        return None

    async def run(self, thread_id, message, user):
        async for e in self._stream(thread_id, user, {"messages": [HumanMessage(message)]}, "message", {"message": message}):
            yield e

    async def resume(self, thread_id, decision, reason, user):
        async for e in self._stream(thread_id, user, Command(resume={"decision": decision, "reason": reason}), "resume",
                                    {"decision": decision, "reason": reason}):
            yield e

    def health(self):
        import time
        now = time.monotonic()
        ages = [now - t for t in list(self.inflight.values())]
        return {"turns_in_flight": len(ages), "turns_stuck": sum(a > TURN_TIMEOUT_S for a in ages),
                "oldest_turn_s": round(max(ages), 1) if ages else 0}

    async def _stream(self, thread_id, user, payload, kind, inputs):
        # the sync graph (sync tools + sync Lakebase checkpointer) runs in a worker thread; events cross over a queue
        import asyncio
        import threading
        import time
        loop, q, done = asyncio.get_running_loop(), asyncio.Queue(), object()
        abandoned = threading.Event()

        def emit(ev):
            loop.call_soon_threadsafe(q.put_nowait, ev)

        def work():
            try:
                for attempt in (1, 2):
                    shown = False
                    try:
                        for ev in self._events(thread_id, user, payload, kind, inputs):
                            if abandoned.is_set():
                                return
                            shown = shown or ev.get("type") != "_trace"
                            emit(ev)
                        return
                    except Exception as e:
                        # Lakebase dropped the connection before the user saw anything: run the turn once more
                        if attempt == 1 and not shown and is_transient(e) and not abandoned.is_set():
                            logger.warning("turn failed on a dropped Lakebase connection before any output; retrying once")
                            continue
                        # no traceback: frames hold user messages, tool rows and tokens
                        logger.error("agent turn failed: %s", _scrub(e))
                        emit({"type": "error", "message": "The agent hit a problem answering this. Try again."})
                        return
            finally:
                emit(done)

        key = object()
        self.inflight[key] = time.monotonic()
        fut = loop.run_in_executor(None, work)
        trace_id, span, timed_out = None, None, False
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=TURN_TIMEOUT_S)
                except asyncio.TimeoutError:
                    timed_out = True
                    abandoned.set()
                    logger.error("agent turn produced nothing for %.0f s; abandoning it (trace %s)", TURN_TIMEOUT_S, trace_id)
                    _close_error(span, f"no progress for {TURN_TIMEOUT_S:.0f} s")
                    yield {"type": "error", "message": "The agent took too long to answer. Try again."}
                    break
                if ev is done:
                    break
                if ev.get("type") == "_trace":
                    trace_id, span = ev["trace_id"], ev.get("span")
                    continue
                yield ev
            if not timed_out:
                await fut
        finally:
            self.inflight.pop(key, None)
        yield {"type": "final", **({"trace_id": trace_id} if trace_id else {})}

    def events(self, thread_id, user, payload, kind, inputs):
        # public sync stream on the caller's thread (eval, scripts): each step runs in the caller's contextvars,
        # so a turn is its own trace unless the caller has an open span; the span object stays internal
        for ev in self._events(thread_id, user, payload, kind, inputs):
            yield {"type": "_trace", "trace_id": ev["trace_id"]} if ev.get("type") == "_trace" else ev

    def _events(self, thread_id, user, payload, kind, inputs):
        graph = self._graph(user)
        cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
        args_by_id, answer, streamed = {}, [], False
        with turn_span("bev_ops_turn", thread_id, user.email, kind, inputs) as span:
            if span.trace_id and "NO_OP" not in span.trace_id:
                yield {"type": "_trace", "trace_id": span.trace_id, "span": span}
            for mode, data in graph.stream(payload, cfg, stream_mode=["messages", "updates"]):
                if mode == "messages":
                    chunk, meta = data
                    if meta.get("langgraph_node") == "agent" and isinstance(chunk, AIMessageChunk):
                        t = text_of(chunk)
                        if t:
                            streamed = True
                            answer.append(t)
                            yield {"type": "token", "text": t}
                    continue
                for node, upd in (data or {}).items():
                    if node == "__interrupt__":
                        st = graph.get_state(cfg)
                        req = _enrich(upd[0].value["request"], st.values.get("messages", []))
                        left = len(st.values.get("pending_queue") or [])
                        t = _card_intro(req, left, resumed=kind == "resume", after_text=bool("".join(answer).strip()))
                        answer.append(t)
                        yield {"type": "token", "text": t}
                        yield {"type": "approval_required", "request": _jsonable(req)}
                    elif node == "check_proposal" and (upd or {}).get("submitted"):
                        reqs = [_jsonable(_enrich(r, [])) for r in upd["submitted"]]
                        yield {"type": "requests_submitted", "requests": reqs}
                    elif node == "agent":
                        for m in (upd or {}).get("messages", []):
                            if not streamed and not getattr(m, "tool_calls", None):
                                # model did not stream tokens: send the final text once
                                t = text_of(m)
                                if t:
                                    answer.append(t)
                                    yield {"type": "token", "text": t}
                            elif streamed and text_of(m).endswith(TRUNCATED_NOTE):
                                # the cut-short note is added after streaming: send it too
                                answer.append(TRUNCATED_NOTE)
                                yield {"type": "token", "text": TRUNCATED_NOTE}
                            streamed = False
                            if getattr(m, "tool_calls", None):
                                # text written before a tool call is a preamble, not the answer (UI clears it on tool_start)
                                answer.clear()
                            for tc in getattr(m, "tool_calls", None) or []:
                                args_by_id[tc["id"]] = tc["args"]
                                yield {"type": "tool_start", "id": tc["id"], "tool": tc["name"], "kind": KIND.get(tc["name"], "uc_function"),
                                       "label": LABEL.get(tc["name"], tc["name"]), "input": _jsonable(tc["args"])}
                    elif node == "tools":
                        for m in (upd or {}).get("messages", []):
                            if isinstance(m, ToolMessage):
                                yield _jsonable(_tool_end(m, args_by_id.get(m.tool_call_id)))
                    elif node == "approval" and (upd or {}).get("last_decision"):
                        yield {"type": "decision", **_jsonable(upd["last_decision"])}
            span.set_outputs({"answer": "".join(answer)})


def _close_error(span, why):
    # end the root span from the event loop when the worker is stuck; the worker's own end later is a no-op
    if span is None:
        return
    try:
        span.set_attribute("bev.abandoned", why)
        span.end(status="ERROR")
    except Exception:
        logger.exception("could not close the abandoned turn trace")


class _NoCtx:
    # graph structure only (state reads); tools are never executed with it
    wc = None
    user_email = ""

    def pg(self, role):
        raise RuntimeError("no request context")

    def embed(self, texts):
        raise RuntimeError("no request context")
