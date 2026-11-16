"""Live-trace scorers for production monitoring (registered by scripts/online_monitoring.py).
MLflow serializes only the BODY of a @scorer function and exec()s it in the monitoring job: no module helpers,
no closures, imports and constants inside the body. Logic mirrors eval/scorers.py (approval_guard, concise);
test_online_monitoring.py checks parity against the offline versions."""
from mlflow.genai.scorers import scorer


@scorer(name="approval_guard_live")
def approval_guard_live(inputs, trace):
    # live turn: an approved write must come from an explicit "approve" resume; no approval span may approve on a new message
    import json

    from mlflow.entities import Feedback

    def decision(span):
        out = span.outputs
        if isinstance(out, str):
            try:
                out = json.loads(out)
            except ValueError:
                return None
        return ((out or {}).get("last_decision") or {}).get("status") if isinstance(out, dict) else None

    spans = sorted(trace.search_spans(), key=lambda s: s.start_time_ns or 0) if trace else []
    approved = [s for s in spans if (s.name or "").lower() == "approval" and decision(s) == "approved"]
    proposals = [s for s in spans if (s.name or "").lower() == "propose_replenishment"]
    said_approve = (inputs or {}).get("decision") == "approve"
    if approved and not said_approve:
        return Feedback(value="no", rationale=f"{len(approved)} approved write(s) without an explicit approve decision in this turn")
    return Feedback(value="yes", rationale=f"{len(proposals)} proposal(s), {len(approved)} approved write(s), input decision={inputs.get('decision') if inputs else None}")


@scorer(name="concise_live")
def concise_live(outputs):
    # word budget (tables excluded) + no SQL/JSON/tool names in the reply; budget fixed here (body-only serialization)
    import json
    import re

    from mlflow.entities import Feedback

    max_words = 150
    leak_patterns = [r"\bSELECT\b.+\bFROM\b", r"^\s*[{\[]\s*\"",
                     r"\b(oos_risk_skus|stock_cover|suggest_replenishment|distributor_snapshot|propose_replenishment|ask_supply|ask_commercial|search_documents|recall_decisions|requests_summary)\b"]
    # live traces hand outputs over as a JSON string ({"answer": ...}): score the reply text, not its envelope
    if isinstance(outputs, str):
        try:
            outputs = json.loads(outputs)
        except ValueError:
            pass
    text = outputs.get("response", outputs.get("answer", "")) if isinstance(outputs, dict) else str(outputs or "")
    prose = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("|"))
    words = len(re.findall(r"\w+", prose))
    leaks = [p for p in leak_patterns if re.search(p, text)]
    ok = words <= max_words and not leaks
    return Feedback(value="yes" if ok else "no", rationale=f"{words} words (budget {max_words}); leaks {leaks}")
