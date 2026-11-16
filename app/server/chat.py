"""Chat API: threads (Lakebase agent_ui, per user) + turns streamed as Server-Sent Events over POST.
Each turn runs as a server task (one per conversation) folded and persisted server-side; streams only subscribe, so switching
conversation never cancels it and a reopened thread re-attaches (GET /chat/stream/{id}).
"""
import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from server import settings as S
from memory.pgretry import retrying
from server import ui_store as store
from server.agent_bridge import make_bridge
from server.identity import User, clean, current_user, require_role
from server.ratelimit import TokenBucket
from agent.tracing import keyed_id

logger = logging.getLogger(__name__)
router = APIRouter()
bridge = None  # set by the FastAPI lifespan (start_bridge)


def start_bridge():
    global bridge
    bridge = make_bridge(S.AGENT_MODE)
    if hasattr(bridge, "open"):
        bridge.open()
    return bridge


def stop_bridge():
    global bridge
    if bridge is not None and hasattr(bridge, "close"):
        bridge.close()
    bridge = None

MAX_MESSAGE = 4000


class ChatIn(BaseModel):
    thread_id: str
    message: str = Field(min_length=1, max_length=MAX_MESSAGE)


class ResumeIn(BaseModel):
    thread_id: str
    decision: str
    reason: str | None = Field(default=None, max_length=500)


class FeedbackIn(BaseModel):
    thread_id: str
    trace_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9:/._\-]+$")
    rating: str
    comment: str | None = Field(default=None, max_length=500)


class ThreadIn(BaseModel):
    title: str | None = Field(default=None, max_length=120)


class TitleIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)


def agent_thread(user: User, thread_id: str) -> str:
    # agent-side key (checkpointer + trace session): namespaced per user on top of the ownership check. Keyed with the
    # secret trace salt, so a session id in a trace cannot be matched to an email by hashing candidate addresses.
    return f"{keyed_id(user.email, 12)}-{thread_id}"


async def _owned(user: User, thread_id: str, create: bool = False):
    if not store.is_uuid(thread_id):
        raise HTTPException(400, "Invalid thread id")
    t = await run_in_threadpool(store.get_thread, user, thread_id)
    if t is None and create:
        t = await run_in_threadpool(store.create_thread, user, None, thread_id)
    if t is None:  # someone else's id collides, or deleted: indistinguishable from missing
        raise HTTPException(404, "Conversation not found")
    return t


class _Turn:
    """Folds stream events into the persisted message shape (mirrors the frontend reducer)."""

    def __init__(self):
        self.text, self.tools, self.approval, self.error, self.decision, self.trace_id = "", [], None, None, None, None

    def apply(self, e: dict):
        t = e.get("type")
        if t == "token":
            self.text += e.get("text", "")
        elif t == "tool_start":
            self.text = ""  # preamble before a tool call is not part of the answer
            self.tools.append({k: e.get(k) for k in ("id", "tool", "kind", "label", "input")} | {"status": "running"})
        elif t == "tool_end":
            for x in self.tools:
                if x["id"] == e.get("id"):
                    x.update(status="ok" if e.get("ok") else "error", summary=e.get("summary"),
                             sources=e.get("sources"), rows=e.get("rows"))
        elif t == "approval_required":
            self.approval = {"request": e.get("request"), "state": "pending"}
        elif t == "requests_submitted":
            # four-eyes: informational cards, an approver decides them on the Requests page
            reqs = e.get("requests") or []
            if reqs:
                self.approval = {"request": reqs[0], "requests": reqs, "state": "submitted"}
        elif t == "decision":
            self.decision = e
        elif t == "error":
            self.error = e.get("message")
        elif t == "final":
            self.trace_id = e.get("trace_id")

    def record(self):
        for x in self.tools:
            if x["status"] == "running":
                x["status"] = "error"  # stream ended before the tool returned
        seen, sources = set(), []
        for s in (s for x in self.tools for s in (x.get("sources") or []) if s.get("kind") == "doc"):
            if (s.get("doc_id"), s.get("section")) not in seen:
                seen.add((s.get("doc_id"), s.get("section")))
                sources.append(s)
        return {"text": self.text, "tools": self.tools, "sources": sources, "approval": self.approval, "error": self.error,
                "trace_id": self.trace_id}


BUSY_MSG = "Still answering the previous message in this conversation. Wait for it to finish."
RATE_MSG = "Too many messages in a short time. Wait a few seconds and try again."
chat_limiter = TokenBucket(S.CHAT_RATE_PER_MIN)
LIVE_GRACE_S = 30  # a finished turn stays replayable briefly (thread reopened between its last event and the reload)


class _Live:
    """One running turn: events buffered for replay, stream clients are subscribers."""

    def __init__(self):
        self.events, self.done, self.cond, self.task = [], False, asyncio.Condition(), None

    async def push(self, e):
        async with self.cond:
            self.events.append(e)
            self.cond.notify_all()

    async def finish(self):
        async with self.cond:
            self.done = True
            self.cond.notify_all()


_live: dict[str, _Live] = {}  # agent thread id -> turn (single process: one uvicorn worker)


def _busy(key: str) -> bool:
    live = _live.get(key)
    return live is not None and not live.done


def _reserve(key: str) -> _Live:
    # claim the conversation synchronously (no await between the check and the claim): two concurrent POSTs on the
    # same thread cannot both pass, so they never race on one checkpoint or double the per-turn proposal cap
    if _busy(key):
        raise HTTPException(409, BUSY_MSG)
    live = _Live()
    _live[key] = live
    return live


def _release(key: str, live: _Live):
    # a reserved turn that never started (validation error, rate limit, store failure) frees the conversation
    if _live.get(key) is live and live.task is None:
        _live.pop(key, None)


async def _run_turn(user: User, thread_id: str, key: str, live: _Live, events, on_decision):
    # runs as a server task: a client disconnect never cancels the agent or the persist
    turn = _Turn()
    try:
        async for e in events:
            turn.apply(e)
            if e.get("type") == "decision" and on_decision:
                try:
                    await on_decision(e)
                except Exception as x:
                    logger.error("decision state update failed: %s", clean(str(x)))
            await live.push(e)
    except asyncio.CancelledError:  # app shutdown
        turn.error = turn.error or "Stopped before the answer finished."
        raise
    except Exception as e:
        logger.error("agent stream failed: %s", clean(str(e)))
        turn.error = "The assistant hit an error. Try again."
        await live.push({"type": "error", "message": turn.error})
    finally:
        rec = turn.record()
        if rec["text"] or rec["tools"] or rec["error"]:
            try:
                await asyncio.shield(run_in_threadpool(store.add_assistant_message, user, thread_id, rec))
            except BaseException as e:
                logger.error("persisting assistant turn failed: %s", clean(str(e)))
        # persisted before done: a reload sees either the live turn or the stored answer, never neither
        await live.finish()
        asyncio.get_running_loop().call_later(LIVE_GRACE_S, lambda: _live.pop(key, None) if _live.get(key) is live else None)


def _follow(live: _Live):
    async def gen():
        i = 0
        while True:
            async with live.cond:
                await live.cond.wait_for(lambda: len(live.events) > i or live.done)
                batch, done = live.events[i:], live.done
            i += len(batch)
            for e in batch:
                yield f"data: {json.dumps(e, default=str)}\n\n"
            if done and i == len(live.events):
                return
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})


def _start(user: User, thread_id: str, key: str, live: _Live, events, on_decision=None):
    live.task = asyncio.create_task(_run_turn(user, thread_id, key, live, events, on_decision))
    return _follow(live)


# ---------- threads ----------

@router.get("/threads")
async def threads(request: Request, q: str | None = None):
    u = await run_in_threadpool(current_user, request)
    require_role(u)
    return {"threads": await run_in_threadpool(store.list_threads, u, (q or "").strip()[:80] or None)}


@router.post("/threads")
async def new_thread(body: ThreadIn, request: Request):
    u = await run_in_threadpool(current_user, request)
    require_role(u)
    return await run_in_threadpool(store.create_thread, u, body.title)


@router.get("/threads/{thread_id}/messages")
async def thread_messages(thread_id: str, request: Request):
    u = await run_in_threadpool(current_user, request)
    t = await _owned(u, thread_id)
    msgs = await run_in_threadpool(store.messages, u, thread_id)
    return {"thread": t, "messages": msgs, "running": _busy(agent_thread(u, thread_id))}


@router.patch("/threads/{thread_id}")
async def rename(thread_id: str, body: TitleIn, request: Request):
    u = await run_in_threadpool(current_user, request)
    await _owned(u, thread_id)
    await run_in_threadpool(store.rename_thread, u, thread_id, body.title.strip())
    return {"ok": True}


@router.delete("/threads/{thread_id}")
async def delete(thread_id: str, request: Request):
    u = await run_in_threadpool(current_user, request)
    await _owned(u, thread_id)
    await run_in_threadpool(store.delete_thread, u, thread_id)
    return {"ok": True}


# ---------- turns ----------

@router.post("/chat")
async def chat(body: ChatIn, request: Request):
    u = await run_in_threadpool(current_user, request)
    require_role(u)
    await _owned(u, body.thread_id, create=True)
    key = agent_thread(u, body.thread_id)
    # one turn per conversation: a second one would race the first on the same checkpoint
    live = _reserve(key)
    try:
        # each turn costs LLM and warehouse calls: bound them per user
        if not chat_limiter.allow(u.email.lower()):
            raise HTTPException(429, RATE_MSG)
        msg = body.message.strip()
        # moving on with a card still pending: the card now points at the Requests queue (the request stays proposed)
        await run_in_threadpool(store.queue_pending_cards, u, body.thread_id)
        await run_in_threadpool(store.add_user_message, u, body.thread_id, msg)
        return _start(u, body.thread_id, key, live, bridge.run(key, msg, u))
    finally:
        _release(key, live)


@router.get("/chat/stream/{thread_id}")
async def attach(thread_id: str, request: Request):
    # reopened conversation: replay + follow the turn still running (204 = nothing live)
    u = await run_in_threadpool(current_user, request)
    await _owned(u, thread_id)
    live = _live.get(agent_thread(u, thread_id))
    if live is None:
        return Response(status_code=204)
    return _follow(live)


@retrying
def _request_row(request_id):
    from memory.replenishment import get_request
    from server.auth import service_connection
    with service_connection() as conn:
        return get_request(conn, request_id)


def check_decider(u: User, row, decision: str):
    # four-eyes + manager threshold, before anything is written (memory.replenishment enforces them again)
    from memory.replenishment import needs_manager
    if row is None:
        raise HTTPException(404, "Request not found")
    if row["status"] != "proposed":
        raise HTTPException(409, f"This request is already {row['status']}")
    if not S.ALLOW_SELF_APPROVAL and (row["requested_by"] or "").lower() == u.email.lower():
        raise HTTPException(403, "You proposed this request; another approver must decide it (Requests page)")
    if decision == "approve" and needs_manager(row["qty_cases"]) and not u.is_manager:
        raise HTTPException(403, f"Above {S.MANAGER_THRESHOLD_CASES:g} cases a manager ({S.MANAGER_GROUP}) must approve")


@router.post("/chat/resume")
async def resume(body: ResumeIn, request: Request):
    u = await run_in_threadpool(current_user, request)
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    if not u.can_approve:
        raise HTTPException(403, f"Only members of {S.APPROVER_GROUP} can approve or reject requests")
    await _owned(u, body.thread_id)
    atid = agent_thread(u, body.thread_id)
    live = _reserve(atid)
    try:
        saved = await run_in_threadpool(store.pending_approval, u, body.thread_id)
        if bridge.pending_approval(atid) is None:
            # e.g. app restarted: the graph resumes from its checkpointer; the stub re-arms from the persisted card
            if saved is None or not hasattr(bridge, "restore_pending"):
                raise HTTPException(409, "Nothing is waiting for approval in this conversation")
            bridge.restore_pending(atid, saved["approval"]["request"])
        pending = bridge.pending_approval(atid) or (saved or {}).get("approval", {}).get("request") or {}
        if store.is_uuid(str(pending.get("request_id") or "")):  # stub previews have no stored request
            row = await run_in_threadpool(_request_row, str(pending["request_id"]))
            check_decider(u, row, body.decision)

        async def on_decision(e):
            if saved:
                await run_in_threadpool(store.set_approval_state, u, saved["id"], e["status"], e.get("decided_by"))

        return _start(u, body.thread_id, atid, live,
                      bridge.resume(atid, body.decision, (body.reason or "").strip() or None, u), on_decision)
    finally:
        _release(atid, live)


@router.post("/chat/feedback")
async def feedback(body: FeedbackIn, request: Request):
    u = await run_in_threadpool(current_user, request)
    if body.rating not in ("up", "down"):
        raise HTTPException(400, "rating must be up or down")
    if not hasattr(bridge, "feedback"):
        raise HTTPException(409, "Feedback needs the live agent (preview mode has no traces)")
    await _owned(u, body.thread_id)
    comment = (body.comment or "").strip() or None
    prev = await run_in_threadpool(store.get_feedback, u, body.thread_id, body.trace_id) or {}
    fb = {"rating": body.rating, "comment": comment, "assessment_id": prev.get("assessment_id")}
    if not await run_in_threadpool(store.set_feedback, u, body.thread_id, body.trace_id, fb):
        raise HTTPException(404, "Message not found")
    try:
        # a re-rate overrides the earlier assessment instead of adding a conflicting one
        aid = await run_in_threadpool(bridge.feedback, body.trace_id, body.rating == "up", comment, u.email, fb["assessment_id"])
    except Exception as e:
        logger.error("log_feedback failed: %s", clean(str(e)))
        raise HTTPException(502, "Feedback saved, but it could not be attached to the trace")
    if aid and aid != fb["assessment_id"]:
        await run_in_threadpool(store.set_feedback, u, body.thread_id, body.trace_id, fb | {"assessment_id": aid})
    return {"ok": True}


class DecisionIn(BaseModel):
    decision: str
    reason: str | None = Field(default=None, max_length=500)


@router.post("/requests/{request_id}/decision")
async def decide_request(request_id: str, body: DecisionIn, request: Request):
    # approver queue: decide a request from the Requests page (never the requester; manager rule above the threshold)
    u = await run_in_threadpool(current_user, request)
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    if not u.can_approve:
        raise HTTPException(403, f"Only members of {S.APPROVER_GROUP} can approve or reject requests")
    if not store.is_uuid(request_id):
        raise HTTPException(400, "Invalid request id")
    row = await run_in_threadpool(_request_row, request_id)
    check_decider(u, row, body.decision)
    reason = (body.reason or "").strip() or ("Approved in the request queue" if body.decision == "approve" else "Rejected in the request queue")

    def work():
        from memory.replenishment import approve_request, reject_request
        from rag.search import embed
        from server.auth import service_connection
        wc = u.client()
        emb = lambda texts: embed(texts, wc.config.host, wc.config.authenticate(), S.EMBEDDING_SERVICE)
        fn = approve_request if body.decision == "approve" else reject_request
        with service_connection() as conn:
            res = fn(conn, request_id, u.email, reason, embed_fn=emb, allow_self_approval=S.ALLOW_SELF_APPROVAL, manager_ok=u.is_manager)
        store.set_card_state_for_request(request_id, res["status"], u.email)
        return res

    try:
        res = await run_in_threadpool(work)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError:
        raise HTTPException(409, "This request is no longer waiting for a decision")
    except Exception as e:
        logger.error("decision failed: %s", clean(str(e)))
        raise HTTPException(502, "Could not record the decision")
    return {"ok": True, "status": res["status"], "request_id": str(res["request_id"]), "decided_by": res["decided_by"]}
