"""LangGraph agent: route (llm_fast) -> agent (fast|reasoning, tools) -> tools -> approval interrupt -> agent."""
import json
import os
from typing import Literal, Optional

from databricks_langchain import ChatDatabricks
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from agent.context import LLM_FAST, LLM_REASONING
from agent.prompts import router_prompt, system_prompt
from agent.tools import _people, build_tools


class Route(BaseModel):
    complexity: Literal["basic", "complex"] = Field(description="complex = multi-step analysis, what-if, cross-tool synthesis")
    intent: Literal["lookup", "analysis", "why", "action", "memory", "smalltalk", "out_of_scope"]


class State(MessagesState):
    route: Optional[dict] = None
    pending_request: Optional[dict] = None
    # further proposals from the same tool batch, decided one card at a time
    pending_queue: Optional[list] = None
    # proposals sent to the Requests queue this turn (four-eyes: the requester can't decide them)
    submitted: Optional[list] = None
    # proposals left undecided when the user moved on (still 'proposed' in the queue)
    open_requests: Optional[list] = None
    last_decision: Optional[dict] = None


# models that reject temperature (e.g. claude-sonnet-5); comma-separated service names
# output budgets (reasoning models spend part of it thinking)
MAX_TOKENS_FAST = int(os.environ.get("BEV_MAX_TOKENS_FAST", "1500"))
MAX_TOKENS_REASONING = int(os.environ.get("BEV_MAX_TOKENS_REASONING", "4000"))
TRUNCATED_NOTE = "\n\n_(Answer cut short. Ask me to continue or narrow the question.)_"
# tool rounds per user turn before the agent must answer with what it has
MAX_TOOL_ROUNDS = int(os.environ.get("BEV_MAX_TOOL_ROUNDS", "3"))
BUDGET_NOTE = "Tool budget for this turn is used up. Answer now from the tool results above; say what is still unknown."
NO_TEMPERATURE = set(filter(None, os.environ.get("BEV_NO_TEMPERATURE_SERVICES", LLM_REASONING).split(",")))


def _llm(model, ctx, max_tokens=1200, step="agent"):
    from agent.gateway_tags import header
    kw = {} if model in NO_TEMPERATURE else {"temperature": 0}
    # request tags ride as OpenAI extra_headers (passed through extra_params)
    return ChatDatabricks(model=model, use_ai_gateway=True, workspace_client=ctx.wc, max_tokens=max_tokens,
                          extra_params={"extra_headers": header(step)}, **kw)


def _clean_err(e, n=400):
    import re
    msg = re.sub(r"https?://\S+", "<url>", str(e))
    msg = re.sub(r"\b[0-9a-f]{32}\b|\b[0-9a-f-]{36}\b", "<id>", msg)
    return f"{type(e).__name__}: {msg[:n]}"


def _cached_system(text):
    # stable prefix (system + tool defs) cached at the Gateway
    return SystemMessage(content=[{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}])


def _last_human(messages):
    return next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)


def _mark_truncated(msg):
    # a reply that hit max_tokens tells the user instead of ending mid-sentence
    meta = getattr(msg, "response_metadata", None) or {}
    if meta.get("finish_reason") not in ("length", "max_tokens") or msg.tool_calls:
        return msg
    import logging
    logging.getLogger(__name__).warning("reply hit max_tokens (%s)", meta.get("model_name"))
    content = msg.content
    if isinstance(content, list):
        content = content + [{"type": "text", "text": TRUNCATED_NOTE}]
    else:
        content = (content or "") + TRUNCATED_NOTE
    return AIMessage(content=content, id=msg.id, response_metadata=meta, usage_metadata=msg.usage_metadata)


OPEN_NOTE = ("[context] Replenishment requests proposed earlier in this conversation are still waiting (status proposed) in the "
             "Requests queue. If the user wants to change or drop them, use amend_request or withdraw_request: ")


def _brief(r):
    return {k: r.get(k) for k in ("request_id", "distributor_id", "sku", "product_name", "qty_cases")}


def _next(state):
    # pop the next card of the same batch, if any
    q = list(state.get("pending_queue") or [])
    return {"pending_request": q[0] if q else None, "pending_queue": q[1:] or None}


def _tool_rounds(messages):
    # AI tool-call rounds since the user's last message (approval notes don't reset the count)
    n = 0
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and not str(m.content).startswith("[approval]"):
            break
        if isinstance(m, AIMessage) and m.tool_calls:
            n += 1
    return n


# why/policy questions: the documents are searched first, so no clarifying question comes before the search
DOC_FIRST_INTENTS = {"why"}
DOC_TOOL = "search_documents"


def _doc_first(route, messages):
    return (route or {}).get("intent") in DOC_FIRST_INTENTS and _tool_rounds(messages) == 0


def build_graph(ctx, checkpointer=None):
    tools = build_tools(ctx)
    # no hosts, ids or SQL from raw exceptions reach the model (and through it, the user)
    tool_node = ToolNode(tools, handle_tool_errors=lambda e: f"Tool error: {_clean_err(e)}")
    sys_text = system_prompt()
    fast = _llm(LLM_FAST, ctx, max_tokens=MAX_TOKENS_FAST)
    reasoning = _llm(LLM_REASONING, ctx, max_tokens=MAX_TOKENS_REASONING)
    agents = {"basic": fast.bind_tools(tools), "complex": reasoning.bind_tools(tools)}
    # first round of a doc-first question: the document search is required
    doc_first = {"basic": fast.bind_tools(tools, tool_choice=DOC_TOOL), "complex": reasoning.bind_tools(tools, tool_choice=DOC_TOOL)}
    # same tool defs (history has tool blocks) but no further calls
    final = {"basic": fast.bind_tools(tools, tool_choice="none"), "complex": reasoning.bind_tools(tools, tool_choice="none")}
    router = _llm(LLM_FAST, ctx, max_tokens=100, step="router").with_structured_output(Route)

    def route(state: State):
        h = _last_human(state["messages"])
        if h is None or str(h.content).startswith("[approval]"):
            return {"route": state.get("route") or {"complexity": "basic", "intent": "action"}}
        r = router.invoke([HumanMessage(router_prompt(str(h.content)))])
        # a new message while a card was pending: the interrupt is gone, the request(s) stay proposed in the queue
        left = [q for q in [state.get("pending_request")] + list(state.get("pending_queue") or []) if q]
        return {"route": r.model_dump(), "pending_request": None, "pending_queue": None, "submitted": None,
                "open_requests": [_brief(q) for q in left] or None}

    def agent(state: State):
        tier = (state.get("route") or {}).get("complexity", "basic")
        msgs = [_cached_system(sys_text)] + state["messages"]
        if state.get("open_requests"):
            # transient context (not persisted): what is still waiting from earlier in the thread
            msgs = msgs + [HumanMessage(OPEN_NOTE + json.dumps(state["open_requests"]))]
        if _tool_rounds(state["messages"]) >= MAX_TOOL_ROUNDS:
            out = final[tier].invoke(msgs + [HumanMessage(BUDGET_NOTE)])
            # guard: a model that ignores tool_choice still ends the turn
            if out.tool_calls:
                out = AIMessage(content=out.content, id=out.id)
            return {"messages": [_mark_truncated(out)]}
        bound = doc_first[tier] if _doc_first(state.get("route"), state["messages"]) else agents[tier]
        return {"messages": [_mark_truncated(bound.invoke(msgs))]}

    def after_agent(state: State):
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    def after_tools(state: State):
        # clear a decision from an earlier turn
        state_update = {"last_decision": None}
        # every proposal created in the last tool batch
        found = []
        for m in reversed(state["messages"]):
            if not isinstance(m, ToolMessage):
                break
            if m.name == "propose_replenishment":
                try:
                    out = json.loads(m.content)
                except (TypeError, ValueError):
                    continue
                if out.get("status") == "awaiting_approval":
                    found.append(out["request"])
        found.reverse()
        if not found:
            return {**state_update, "pending_request": None}
        if ctx.can_decide_own:
            return {**state_update, "pending_request": found[0], "pending_queue": found[1:] or None}
        note = (f"[approval] {len(found)} request(s) sent to the approvers' queue (status proposed): "
                + "; ".join(f"{r.get('request_id')} {r.get('qty_cases')} cases {r.get('sku')} for {r.get('distributor_id')}" for r in found)
                + ". The user cannot approve their own requests: say an approver decides them on the Requests page.")
        return {**state_update, "pending_request": None, "submitted": found, "messages": [HumanMessage(note)]}

    def route_after_tools(state: State):
        return "approval" if state.get("pending_request") else "agent"

    def route_after_approval(state: State):
        return "approval" if state.get("pending_request") else "agent"

    def approval(state: State):
        # pauses the graph; resumed with Command(resume={"decision": "approve"|"reject", "reason": str})
        req = state["pending_request"]
        ans = interrupt({"type": "approval_required", "request": req})
        decision = (ans or {}).get("decision")
        reason = ((ans or {}).get("reason") or "").strip() or ("Approved in chat" if decision == "approve" else "Rejected in chat")
        from memory.replenishment import approve_request, reject_request
        fn = approve_request if decision == "approve" else reject_request
        try:
            with ctx.write() as conn:
                res = fn(conn, req["request_id"], ctx.user_email, reason, embed_fn=ctx.embed,
                         allow_self_approval=ctx.allow_self_approval, manager_ok=ctx.manager_ok)
        except (PermissionError, ValueError) as e:
            # the app pre-checks four-eyes and the manager rule; this covers races (e.g. decided on the Requests page)
            note = f"[approval] Not recorded: {e}. Tell the user briefly; the request stays in the Requests queue."
            return {"messages": [HumanMessage(note)], **_next(state)}
        # no email in model-facing text: it lands in the LLM span of the trace
        note = f"[approval] The user {'APPROVED' if decision == 'approve' else 'REJECTED'} request {req['request_id']}: {reason}. Result: {json.dumps(_people(res, ctx.user_email), default=str)[:600]}. Confirm briefly to the user."
        status = "approved" if decision == "approve" else "rejected"
        return {"messages": [HumanMessage(note)], **_next(state),
                # graph state lands in node spans: same email masking as tool outputs ("you" / "a colleague (u-...)")
                "last_decision": _people({"status": status, "request": {**req, **(res if isinstance(res, dict) else {})},
                                          "decided_by": ctx.user_email}, ctx.user_email)}

    g = StateGraph(State)
    g.add_node("route", route)
    g.add_node("agent", agent)
    g.add_node("tools", tool_node)
    g.add_node("check_proposal", after_tools)
    g.add_node("approval", approval)
    g.add_edge(START, "route")
    g.add_edge("route", "agent")
    g.add_conditional_edges("agent", after_agent, {"tools": "tools", END: END})
    g.add_edge("tools", "check_proposal")
    g.add_conditional_edges("check_proposal", route_after_tools, {"approval": "approval", "agent": "agent"})
    g.add_conditional_edges("approval", route_after_approval, {"approval": "approval", "agent": "agent"})
    return g.compile(checkpointer=checkpointer)
