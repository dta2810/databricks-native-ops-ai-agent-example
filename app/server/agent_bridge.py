"""Bridge between the HTTP layer and the agent. The real LangGraph graph (agent/) implements AgentBridge; StubBridge is a
scripted preview that calls the real tools (UC functions as the user, Lakebase hybrid search, decision recall) with a fixed plan
and templated wording, so the UI is testable before the graph lands. Event shapes: app/API.md. No ids are hardcoded: every
market, distributor, SKU and promo comes from the data.
"""
import asyncio
import logging
import re
from typing import AsyncIterator, Protocol

from fastapi.concurrency import run_in_threadpool

from server import settings as S
from server import warehouse
from server.identity import User, clean

logger = logging.getLogger(__name__)


class AgentBridge(Protocol):
    mode: str

    def run(self, thread_id: str, message: str, user: User) -> AsyncIterator[dict]:
        """Stream events for one user turn. thread_id is the agent-side key (already namespaced per user)."""

    def resume(self, thread_id: str, decision: str, reason: str | None, user: User) -> AsyncIterator[dict]:
        """Resume a thread paused on approval_required with approve | reject."""

    def pending_approval(self, thread_id: str) -> dict | None:
        """The approval the thread is waiting on, or None (the graph reads this from its checkpointer)."""


async def _tokens(text: str, delay: float = 0.01):
    for piece in re.findall(r"\S+\s*", text):
        yield {"type": "token", "text": piece}
        await asyncio.sleep(delay)


def _sql_source(title, sql):
    return {"kind": "sql", "title": title, "sql": sql}


class StubBridge:
    mode = "stub"

    def __init__(self):
        self._pending: dict[str, dict] = {}

    def pending_approval(self, thread_id):
        return self._pending.get(thread_id)

    def restore_pending(self, thread_id, request):
        self._pending[thread_id] = request

    # ---------- tools (real calls, as the user) ----------

    def _uc(self, user, fn, args: dict, sql_tail=""):
        sql = f"SELECT * FROM {warehouse.fn(fn)}({', '.join(':' + k for k in args)}){sql_tail}"
        rows = warehouse.query(user.client(), sql, args)
        shown = sql
        for k, v in args.items():
            shown = shown.replace(f":{k}", "NULL" if v is None else (str(v) if isinstance(v, float) else f"'{v}'"))
        return rows, shown

    def _docs(self, user, query, market=None):
        from rag.search import embed, search
        w = user.client()
        qvec = embed([query], w.config.host, w.config.authenticate(), S.EMBEDDING_SERVICE)[0]
        with user.connection(user.read_role or "") as conn:
            hits = search(conn, query, qvec, market=market, top_k=3)
        return [{"kind": "doc", "doc_id": h["doc_id"], "section": h.get("section"),
                 "snippet": (h.get("content") or "").split("\n", 1)[-1][:280]} for h in hits]

    def _recall(self, user, market=None, sku=None, distributor_id=None):
        from memory.decisions import recall_decisions
        with user.connection(user.read_role or "") as conn:
            rows = recall_decisions(conn, user.email, market_id=market, sku=sku, distributor_id=distributor_id, top_k=3)
        return [{"kind": "memory", "decision_id": str(r["decision_id"]), "created_at": str(r["created_at"])[:10], "summary": r["summary"]}
                for r in rows]

    # ---------- flows ----------

    async def run(self, thread_id, message, user):
        m = message.lower()
        try:
            if re.search(r"promo|uplift|forecast|campaign", m):
                flow = self._promo(user, message)
            elif re.search(r"stock|oos|cover|run out|running out|replenish|top (?:it )?up|top-up|send|low\b", m):
                flow = self._oos(thread_id, user, message)
            else:
                flow = None
            if flow is None:
                async for e in _tokens("I can help with out-of-stock risk, replenishment requests and in-flight promotions. "
                                       "Try one of the suggested questions."):
                    yield e
            else:
                async for e in flow:
                    yield e
        except Exception as e:
            logger.error("stub flow failed: %s", clean(str(e)))
            yield {"type": "error", "message": "The preview agent couldn't reach the data. Try again."}
        yield {"type": "final"}

    def _match(self, rows, message):
        # pick the at-risk position the message mentions (distributor id, SKU or product words), else the lowest cover
        low = message.lower()
        for r in rows:
            if r["distributor_id"].lower() in low or r["sku"].lower() in low:
                return r
        words = [w for w in re.findall(r"[a-z]{4,}", low)]
        scored = sorted(rows, key=lambda r: -sum(w in (r.get("product_name") or "").lower() for w in words))
        return scored[0] if scored and any(w in (scored[0].get("product_name") or "").lower() for w in words) else rows[0]

    async def _oos(self, thread_id, user, message):
        th = S.OOS_THRESHOLD_DAYS
        yield {"type": "tool_start", "id": "t1", "tool": "oos_risk_skus", "kind": "uc_function", "label": "Checking out-of-stock risk",
               "input": {"market": None, "threshold_days": th}}
        rows, sql = await run_in_threadpool(self._uc, user, "oos_risk_skus", {"market": None, "threshold": float(th)}, " ORDER BY cover_days")
        if not rows:
            yield {"type": "tool_end", "id": "t1", "ok": True, "summary": "No positions at risk", "sources": [_sql_source("UC function oos_risk_skus", sql)]}
            async for e in _tokens(f"No distributor position is below {th:g} days of cover right now."):
                yield e
            return
        pick = self._match(rows, message)
        same = [r for r in rows if r["sku"] == pick["sku"] and r["market_id"] == pick["market_id"]]
        yield {"type": "tool_end", "id": "t1", "ok": True, "summary": f"{len(rows)} positions below {th:g} days of cover",
               "sources": [_sql_source("UC function oos_risk_skus", sql)],
               "rows": [{k: r[k] for k in ("distributor_id", "sku", "cover_days", "weekly_revenue_at_risk_usd")} for r in same[:5]]}

        yield {"type": "tool_start", "id": "t2", "tool": "recall_decisions", "kind": "memory", "label": "Checking past decisions",
               "input": {"distributor_id": pick["distributor_id"], "sku": pick["sku"]}}
        try:
            mem = await run_in_threadpool(self._recall, user, None, pick["sku"], pick["distributor_id"])
            yield {"type": "tool_end", "id": "t2", "ok": True, "summary": f"{len(mem)} earlier decision{'s' if len(mem) != 1 else ''}", "sources": mem}
        except Exception as e:
            logger.warning("recall failed: %s", clean(str(e)))
            mem = []
            yield {"type": "tool_end", "id": "t2", "ok": False, "summary": "Memory unavailable"}

        yield {"type": "tool_start", "id": "t3", "tool": "suggest_replenishment", "kind": "uc_function", "label": "Sizing the replenishment",
               "input": {"distributor_id": pick["distributor_id"], "sku": pick["sku"]}}
        sug, sql2 = await run_in_threadpool(self._uc, user, "suggest_replenishment", {"distributor_id": pick["distributor_id"], "sku": pick["sku"]})
        s = sug[0] if sug else None
        yield {"type": "tool_end", "id": "t3", "ok": bool(s), "summary": f"{s['recommended_qty_cases']:g} cases to reach {s['target_cover_days']:g} days" if s else "No suggestion",
               "sources": [_sql_source("UC function suggest_replenishment", sql2)]}

        yield {"type": "tool_start", "id": "t4", "tool": "search_documents", "kind": "documents", "label": "Reading policy and agreements",
               "input": {"query": f"{pick['product_name']} {pick['distributor_id']} replenishment"}}
        try:
            docs = await run_in_threadpool(self._docs, user, f"{pick['product_name']} {pick['sku']} {pick['distributor_id']} replenishment credit hold allocation", None)
            yield {"type": "tool_end", "id": "t4", "ok": True, "summary": f"{len(docs)} passages", "sources": docs}
        except Exception as e:
            logger.warning("doc search failed: %s", clean(str(e)))
            docs = []
            yield {"type": "tool_end", "id": "t4", "ok": False, "summary": "Document search unavailable"}

        rejected = next((x for x in mem if x["summary"].lower().startswith("rejected")), None)
        cite = f" [{docs[0]['doc_id']}]" if docs else ""
        text = (f"{pick['product_name']} ({pick['sku']}) is at **{pick['cover_days']:g} days** of cover at {pick['distributor_id']} "
                f"({pick['distributor_name']}), against a {th:g}-day risk threshold; {len(same)} distributor"
                f"{'s' if len(same) != 1 else ''} in {pick['market_id']} {'are' if len(same) != 1 else 'is'} at risk on this SKU.")
        if rejected:
            text += (f"\n\nYou decided on this position before: {rejected['summary']} I won't propose the same request again; "
                     "tell me if the situation changed.")
            async for e in _tokens(text):
                yield e
            return
        if not s:
            async for e in _tokens(text):
                yield e
            return
        text += (f"\n\nThe policy target is {s['target_cover_days']:g} days, so I suggest **{s['recommended_qty_cases']:g} cases**{cite}. "
                 f"{s['policy_note']}\n\nI prepared the request below (preview: nothing is written). Nothing ships until an approver decides.")
        async for e in _tokens(text):
            yield e
        req = {"market_id": pick["market_id"], "distributor_id": pick["distributor_id"], "distributor_name": pick["distributor_name"],
               "sku": pick["sku"], "product_name": pick["product_name"], "qty_cases": s["recommended_qty_cases"],
               "cover_days": pick["cover_days"], "target_cover_days": s["target_cover_days"],
               "needs_manager_approval": bool(s["needs_manager_approval"]),
               "reason": f"Cover {pick['cover_days']:g} d vs {s['target_cover_days']:g} d target." + (f" See {docs[0]['doc_id']}." if docs else "")}
        self._pending[thread_id] = req
        yield {"type": "approval_required", "request": req}

    async def _promo(self, user, message):
        yield {"type": "tool_start", "id": "t1", "tool": "promo_performance", "kind": "uc_function", "label": "Checking in-flight promotions",
               "input": {"status": "Active"}}
        promos, sql = await run_in_threadpool(self._uc, user, "promo_performance", {"market": None, "status": "Active"})
        low = message.lower()
        p = next((x for x in promos if x["promo_id"].lower() in low), promos[0] if promos else None)
        yield {"type": "tool_end", "id": "t1", "ok": True, "summary": f"{len(promos)} active promotion{'s' if len(promos) != 1 else ''}",
               "sources": [_sql_source("UC function promo_performance", sql)]}
        if not p:
            async for e in _tokens("There are no active promotions to measure right now."):
                yield e
            return
        yield {"type": "tool_start", "id": "t2", "tool": "promo_by_distributor", "kind": "uc_function", "label": f"Measuring {p['promo_id']} by distributor",
               "input": {"promo_id": p["promo_id"]}}
        dist, sql2 = await run_in_threadpool(self._uc, user, "promo_by_distributor", {"promo_id": p["promo_id"]}, " ORDER BY uplift_pct")
        yield {"type": "tool_end", "id": "t2", "ok": True, "summary": f"Uplift for {len(dist)} distributors",
               "sources": [_sql_source("UC function promo_by_distributor", sql2)],
               "rows": [{k: d[k] for k in ("distributor_id", "channel", "uplift_pct")} for d in dist]}
        worst = dist[0] if dist else None
        yield {"type": "tool_start", "id": "t3", "tool": "search_documents", "kind": "documents", "label": "Searching briefs and field reports",
               "input": {"query": f"{p['promo_id']} {worst['distributor_id'] if worst else ''}"}}
        try:
            docs = await run_in_threadpool(self._docs, user, f"{p['promo_id']} {worst['distributor_name'] if worst else ''} display execution field report", p["market_id"])
            yield {"type": "tool_end", "id": "t3", "ok": True, "summary": f"{len(docs)} passages", "sources": docs}
        except Exception as e:
            logger.warning("doc search failed: %s", clean(str(e)))
            docs = []
            yield {"type": "tool_end", "id": "t3", "ok": False, "summary": "Document search unavailable"}
        pct = lambda v: f"{v * 100:+.0f}%"
        text = (f"{p['promo_id']} ({p['product_name']}, {p['mechanic']}, {p['discount_pct'] * 100:.0f}% off) is measuring **{pct(p['uplift_actual_pct'])}** "
                f"against a **{pct(p['uplift_forecast_pct'])}** forecast.")
        if worst:
            others = ", ".join(f"{d['distributor_id']} {pct(d['uplift_pct'])}" for d in dist[1:])
            text += f" The gap sits at {worst['distributor_id']} ({worst['distributor_name']}) at **{pct(worst['uplift_pct'])}**" + (f", while {others}." if others else ".")
        if docs:
            text += f"\n\nThe most relevant source is {docs[0]['doc_id']} ({docs[0]['section']}): “{docs[0]['snippet'][:200]}”"
        async for e in _tokens(text):
            yield e

    async def resume(self, thread_id, decision, reason, user):
        req = self._pending.pop(thread_id, None)
        if req is None:
            yield {"type": "error", "message": "Nothing is waiting for approval in this conversation."}
            return
        status = "approved" if decision == "approve" else "rejected"
        yield {"type": "tool_start", "id": "r1", "tool": f"{decision}_request", "kind": "request", "label": "Recording the decision", "input": {"decision": decision}}
        await asyncio.sleep(0.2)
        yield {"type": "tool_end", "id": "r1", "ok": True, "summary": f"Request {status} (preview: not written to Lakebase)"}
        yield {"type": "decision", "status": status, "request": req, "decided_by": user.email}
        async for e in _tokens(f"Done. The {req['qty_cases']:g}-case request for {req['distributor_id']} is **{status}**"
                               + (f" ({reason})" if reason else "") + ". In preview mode nothing is written; the real agent saves the decision to memory."):
            yield e
        yield {"type": "final"}


def make_bridge(mode: str) -> AgentBridge:
    if mode == "graph":
        from agent.bridge import GraphBridge  # owned by the agent workstream
        return GraphBridge()
    return StubBridge()
