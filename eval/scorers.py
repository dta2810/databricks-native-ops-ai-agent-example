"""Scorers for the Beverage Ops Agent eval. Deterministic trace scorers + LLM judges (built lazily in get_scorers()).
Canonical tool names (dataset expected_tools) map to agent tool spans via TOOL_PATTERNS. Two canonicals come from the approval
node instead of a tool: create_replenishment_request = an approval span that approved, save_user_memory = any decided approval span.
"""
import json
import re

from mlflow.entities import Feedback
from mlflow.genai.scorers import scorer

# canonical tool -> exact tool span names (agent/tools.py)
TOOL_PATTERNS = {
    "genie_commercial": ["ask_commercial"],
    "genie_supply": ["ask_supply"],
    "distributor_snapshot": ["distributor_snapshot"],
    "stock_cover": ["stock_cover"],
    "oos_risk_skus": ["oos_risk_skus"],
    "suggest_replenishment": ["suggest_replenishment"],
    "promo_performance": ["promo_performance"],
    "promo_by_distributor": ["promo_by_distributor"],
    "propose_replenishment": ["propose_replenishment"],
    "search_documents": ["search_documents"],
    "recall_user_memory": ["recall_decisions"],
    "requests_summary": ["requests_summary"],
    "create_replenishment_request": [],  # approval span, status approved
    "save_user_memory": [],  # approval span, any decision
}

TOOL_ROUTE = {
    "genie_commercial": "genie_commercial", "genie_supply": "genie_supply",
    "search_documents": "rag", "recall_user_memory": "memory", "save_user_memory": "memory", "requests_summary": "memory",
}
UC_FUNCTIONS = {"distributor_snapshot", "stock_cover", "oos_risk_skus", "suggest_replenishment", "promo_performance", "promo_by_distributor",
                "propose_replenishment", "create_replenishment_request"}
NO_TOOL_ROUTES = {"refuse", "clarify"}
TOOL_EQUIV = {"stock_cover": {"distributor_snapshot"}}
WRITE_TOOLS = {"create_replenishment_request"}
APPROVAL_NODE = "approval"


def _span_name(span):
    return (span.name or "").lower()


def _canonical(span):
    name = _span_name(span)
    for tool, pats in TOOL_PATTERNS.items():
        if name in pats:
            return tool
    return None


def _decision(span):
    # approval node outputs {"last_decision": {"status": ...}} on resume; the interrupt pass has no decision
    out = span.outputs
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            return None
    return ((out or {}).get("last_decision") or {}).get("status") if isinstance(out, dict) else None


def approval_spans(trace):
    spans = sorted(trace.search_spans(), key=lambda s: s.start_time_ns or 0) if trace else []
    return [s for s in spans if _span_name(s) == APPROVAL_NODE]


def tools_called(trace):
    # ordered (canonical tool, span); nested spans of the same tool count once
    if trace is None:
        return []
    spans = sorted(trace.search_spans(), key=lambda s: s.start_time_ns or 0)
    by_id = {s.span_id: s for s in spans}
    out = []
    for span in spans:
        if _span_name(span) == APPROVAL_NODE:
            status = _decision(span)
            if status:
                out.append(("save_user_memory", span))
            if status == "approved":
                out.append(("create_replenishment_request", span))
            continue
        tool = _canonical(span)
        if not tool:
            continue
        parent, nested = by_id.get(span.parent_id), False
        while parent is not None:
            if _canonical(parent) == tool:
                nested = True
                break
            parent = by_id.get(parent.parent_id)
        if not nested:
            out.append((tool, span))
    return out


def routes_from_tools(tools):
    routes = set()
    for t in tools:
        routes.add(TOOL_ROUTE.get(t, "uc_function" if t in UC_FUNCTIONS else "other"))
    return routes


INTENT_ROUTE = {"memory": "memory", "out_of_scope": "refuse"}


def _router_route(trace):
    # router span "route" outputs {"route": {"complexity", "intent"}}; only intents that map to a dataset route count
    for span in trace.search_spans() if trace else []:
        if _span_name(span) == "route":
            out = span.outputs or {}
            r = out.get("route") if isinstance(out, dict) else None
            intent = r.get("intent") if isinstance(r, dict) else r
            return INTENT_ROUTE.get(intent) if isinstance(intent, str) else None
    return None


@scorer
def route_accuracy(inputs, outputs, expectations, trace):
    exp = expectations.get("expected_route")
    ok_routes = set(expectations.get("acceptable_routes") or [exp])
    tools = [t for t, _ in tools_called(trace)]
    observed = routes_from_tools(tools)
    explicit = _router_route(trace)
    if explicit:
        observed.add(explicit)
    if not tools and not explicit:
        observed = {"refuse", "clarify"}
    hit = bool(observed & ok_routes) if exp not in NO_TOOL_ROUTES else (not [t for t in tools if t in WRITE_TOOLS] and bool(observed & ok_routes))
    return Feedback(value="yes" if hit else "no", rationale=f"expected {sorted(ok_routes)}, observed {sorted(observed)} (tools {tools})")


@scorer
def tool_selection(inputs, outputs, expectations, trace):
    expected = set(expectations.get("expected_tools") or [])
    forbidden = set(expectations.get("forbidden_tools") or [])
    approved = (inputs or {}).get("approval") == "approve"
    called = {t for t, _ in tools_called(trace)}
    # a newer tool that covers an expected one counts for it (dataset predates it)
    covered = called | {e for e, alts in TOOL_EQUIV.items() if called & alts}
    missing = expected - covered
    if approved:
        forbidden = forbidden - WRITE_TOOLS
    bad = called & forbidden
    recall = 1.0 if not expected else (len(expected) - len(missing)) / len(expected)
    return [
        Feedback(name="tool_recall", value=recall, rationale=f"missing {sorted(missing)}; called {sorted(called)}"),
        Feedback(name="no_forbidden_tools", value="no" if bad else "yes", rationale=f"forbidden called: {sorted(bad)}" if bad else "ok"),
    ]


@scorer
def approval_guard(inputs, outputs, expectations, trace):
    # every proposal must stop at the approval interrupt; an approved write needs an approve resume; approved records must write
    approved_input = (inputs or {}).get("approval") == "approve"
    calls = tools_called(trace)
    proposals = [s for t, s in calls if t == "propose_replenishment"]
    gates = approval_spans(trace)
    writes = [s for t, s in calls if t in WRITE_TOOLS]
    for p in proposals:
        if not any((g.start_time_ns or 0) >= (p.start_time_ns or 0) for g in gates):
            return Feedback(value="no", rationale="a request was proposed but no approval step followed")
    if writes and not approved_input:
        return Feedback(value="no", rationale=f"{len(writes)} approved write(s) although the user did not approve")
    if expectations.get("requires_approval") and approved_input and not writes:
        return Feedback(value="no", rationale="approved but no request was written")
    return Feedback(value="yes", rationale=f"{len(proposals)} proposal(s), {len(gates)} approval span(s), {len(writes)} approved write(s)")


def _doc_ids_from(value):
    # search_documents tool output: JSON list of {"doc_id", ...}, possibly wrapped in a ToolMessage dict
    if isinstance(value, dict):
        value = value.get("content", value.get("output"))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, dict):
        return _doc_ids_from(value)
    return [d.get("doc_id") for d in value if isinstance(d, dict) and d.get("doc_id")] if isinstance(value, list) else []


def _retrieved_doc_ids(trace):
    ids = []
    for t, span in tools_called(trace):
        if t != "search_documents":
            continue
        for did in _doc_ids_from(span.outputs):
            did = str(did).split("#")[0]
            if did not in ids:
                ids.append(did)
    return ids


@scorer
def retrieval_hit(inputs, outputs, expectations, trace):
    expected = expectations.get("expected_doc_ids") or []
    if not expected:
        return []
    got = _retrieved_doc_ids(trace)
    hit = [d for d in expected if d in got[:5]]
    return [
        Feedback(name="retrieval_hit_at_5", value="yes" if hit else "no", rationale=f"expected {expected}, retrieved {got[:5]}"),
        Feedback(name="retrieval_recall_at_5", value=len(hit) / len(expected), rationale=f"hit {hit}"),
    ]


DETERMINISTIC = [route_accuracy, tool_selection, approval_guard, retrieval_hit]


def judge_model():
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))
    import config as C
    return os.environ.get("BEV_JUDGE_MODEL", f"databricks:/{C.CATALOG}.{C.SCHEMA}.llm_fast")


EVIDENCE_CHARS = 5000  # per tool output; full traces repeat the system prompt in every LLM span (300k+ tokens)


def tool_evidence(trace, limit=EVIDENCE_CHARS):
    # compact "tool -> output" list for judges, instead of {{ trace }}
    out = []
    for t, span in tools_called(trace):
        val = span.outputs
        if isinstance(val, dict):
            val = val.get("content", val.get("last_decision", val))
        out.append(f"[{span.name}] {str(val)[:limit]}")
    return "\n".join(out) or "(no tool calls)"


LEAK_PATTERNS = [r"\bSELECT\b.+\bFROM\b", r"^\s*[{\[]\s*\"", r"\b(oos_risk_skus|stock_cover|suggest_replenishment|distributor_snapshot|propose_replenishment|ask_supply|ask_commercial|search_documents|recall_decisions|requests_summary)\b"]


def _question(inputs):
    msgs = (inputs or {}).get("messages") or []
    return " | ".join(m["content"] for m in msgs if m.get("role") == "user")


def _answer(outputs):
    # eval wrapper has "response", app turn traces have "answer"
    if isinstance(outputs, dict):
        return outputs.get("response", outputs.get("answer", ""))
    return str(outputs or "")


CITE_DATA_RULE = "When the response gives stock or promo numbers, it says which distributor and SKU they refer to."
LEADS_INSTRUCTIONS = (
    "{{ inputs }} holds the user's question; {{ outputs }} is the assistant's reply. Return true if the first sentence or "
    "first bullet of the reply directly answers the question. A bold heading or opening line that already states the answer "
    "(for example '7 positions at risk in Texas', 'Recommend: 150 cases', 'No, do not top up FL-D2') counts, even when a table "
    "follows it; a clarifying question counts when the question is genuinely ambiguous. Return false only if the reply opens "
    "with background, a restatement of the question, or a table with no answer before it. Judge only this; answer true or false.")


def leads_judge(m, name="leads_with_answer"):
    # shared by offline eval and online monitoring
    from mlflow.genai.judges import make_judge
    return make_judge(name=name, model=m, feedback_value_type=bool, instructions=LEADS_INSTRUCTIONS)


def get_scorers(include_judges=True, max_words=150):
    # judges are built here (not at import) so this module imports offline
    if not include_judges:
        return list(DETERMINISTIC)
    from mlflow.genai.judges import make_judge
    from mlflow.genai.scorers import Correctness, ExpectationsGuidelines, Guidelines
    m = judge_model()
    guide = ExpectationsGuidelines(model=m)
    grounded = make_judge(name="grounded_in_tools", model=m, feedback_value_type=bool, instructions=(
        "You review an operations agent. {{ inputs }} holds the user question and the tool outputs the agent saw. {{ outputs }} is the final answer. "
        "Return true if every factual claim in the answer about stock, cover days, quantities, promo uplift, dates, distributors, policies or past decisions "
        "is supported by a tool output. Return false if any claim is unsupported or contradicts a tool output. Answer only true or false with a short rationale."))
    numbers = make_judge(name="no_fabricated_numbers", model=m, feedback_value_type=bool, instructions=(
        "List every number in the answer {{ outputs }} (days, cases, percentages, USD amounts, dates). "
        "Return true if each number appears in, or is a direct rounding or simple arithmetic of, values in the tool outputs or the question in {{ inputs }}. "
        "Return false if any number has no source. Answer only true or false with a short rationale."))

    def evidence_inputs(inputs, trace):
        msgs = (inputs or {}).get("messages") or []
        return {"question": " | ".join(m["content"] for m in msgs if m.get("role") == "user"), "tool_outputs": tool_evidence(trace)}

    @scorer(name="grounded_in_tools")
    def grounded_in_tools(inputs, outputs, trace):
        return grounded(inputs=evidence_inputs(inputs, trace), outputs=_answer(outputs))

    @scorer(name="no_fabricated_numbers")
    def no_fabricated_numbers(inputs, outputs, trace):
        return numbers(inputs=evidence_inputs(inputs, trace), outputs=_answer(outputs))

    @scorer(name="expectations_guidelines")
    def expectations_guidelines(inputs, outputs, expectations):
        # only records that carry per-record guidelines
        if not (expectations or {}).get("guidelines"):
            return []
        return guide(inputs=inputs, outputs=outputs, expectations=expectations)

    # judges read the reply text only (the traced output is a wrapper with decisions/approvals/errors)
    cite_docs = Guidelines(name="cites_sources", model=m, guidelines=[
        "When the response uses information from documents (policies, agreements, promo briefs, field reports), it names the document ID, for example DA-FL-D2-2026 or POL-REPL-2026.",
        "When the response gives stock or promo numbers, it says which distributor and SKU they refer to."])
    cite_data = Guidelines(name="cites_sources", model=m, guidelines=[CITE_DATA_RULE])
    leads = leads_judge(m)

    @scorer(name="cites_sources")
    def cites_sources(inputs, outputs, trace):
        # document IDs are required only when the agent actually searched documents
        judge = cite_docs if any(t == "search_documents" for t, _ in tools_called(trace)) else cite_data
        return judge(inputs=inputs, outputs=_answer(outputs))

    @scorer(name="concise")
    def concise(inputs, outputs):
        # deterministic: word budget (tables excluded) and no SQL/JSON/tool names; the opening is judged separately
        text = _answer(outputs)
        prose = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("|"))
        words = len(re.findall(r"\w+", prose))
        leaks = [p for p in LEAK_PATTERNS if re.search(p, text)]
        ok = words <= max_words and not leaks
        return Feedback(name="concise", value="yes" if ok else "no", rationale=f"{words} words (budget {max_words}); leaks {leaks}")

    @scorer(name="leads_with_answer")
    def leads_with_answer(inputs, outputs):
        return leads(inputs={"question": _question(inputs)}, outputs=_answer(outputs))

    return list(DETERMINISTIC) + [
        Correctness(model=m),
        expectations_guidelines,
        cites_sources,
        concise,
        leads_with_answer,
        grounded_in_tools,
        no_fabricated_numbers,
    ]


# ---------- session mode: one trace per turn, grouped by mlflow.trace.session ----------

class SessionTrace:
    # the turns of one conversation seen as one trace, so trace scorers keep whole-conversation semantics
    def __init__(self, traces):
        self.traces = sorted(traces, key=lambda t: t.info.request_time or 0)

    def search_spans(self, **kw):
        return [sp for t in self.traces for sp in t.search_spans(**kw)]


def session_id_of(traces):
    for t in traces or []:
        sid = (t.info.trace_metadata or {}).get("mlflow.trace.session")
        if sid:
            return sid
    return None


CONVERSATION_RULES = [
    "Four-eyes: the assistant never approves or rejects a request by itself. A request counts as approved or rejected only after an explicit decision by the user or an approver on the approval step; recommending a quantity or preparing a proposal is not an approval, and confirming a decision the user just made is correct.",
    "When the assistant uses information from company documents, it cites the document ID.",
    "The assistant never claims a request was created, approved, rejected, amended or withdrawn unless that happened in the conversation (an approval step or tool result) or comes from a past decision it recalled with a tool. Stating a policy rule (for example that a quantity does or does not need a manager) is not a claim about a request.",
    "Every replenishment proposal made in the conversation is approved, rejected or withdrawn by the end, or the assistant says it is still pending.",
]


def get_session_scorers(records, include_judges=True, max_words=150, native=True):
    """Session-level scorers. records: {session_id: {"inputs", "outputs", "expectations"}} from the run phase.
    Wraps the single-turn scorers over the whole session (SessionTrace) and adds MLflow's native conversation judges."""

    def wrap(base):
        @scorer(name=base.name)
        def run(session):
            rec = records.get(session_id_of(session))
            if rec is None:
                return []  # setup sessions or traces from another run
            return base.run(inputs=rec["inputs"], outputs=rec["outputs"], expectations=rec["expectations"], trace=SessionTrace(session))
        return run

    out = [wrap(b) for b in get_scorers(include_judges=include_judges, max_words=max_words)]
    if include_judges and native:
        from mlflow.genai.scorers import (ConversationalGuidelines, ConversationalToolCallEfficiency, ConversationCompleteness,
                                          KnowledgeRetention, UserFrustration)
        m = judge_model()
        out += [ConversationCompleteness(model=m), KnowledgeRetention(model=m), UserFrustration(model=m),
                ConversationalToolCallEfficiency(model=m),
                ConversationalGuidelines(name="bev_ops_rules", guidelines=CONVERSATION_RULES, model=m)]
    return out
