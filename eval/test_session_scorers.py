"""Offline tests for the session-mode scorer wrappers (fake turn traces). Run: .venv/bin/python -m pytest -q eval/test_session_scorers.py"""
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))
from scorers import SessionTrace, get_session_scorers, session_id_of  # noqa: E402

SID = "eval-abc123-c2-02-deadbeef"


def span(name, sid, start, outputs=None, parent="root"):
    return SimpleNamespace(name=name, span_id=f"{name}-{start}", parent_id=parent, start_time_ns=start, outputs=outputs, inputs=None,
                           attributes={})


def trace(spans, start, sid=SID):
    info = SimpleNamespace(trace_metadata={"mlflow.trace.session": sid}, request_time=start)
    return SimpleNamespace(info=info, search_spans=lambda **kw: spans)


def turns():
    # turn 1 proposes, turn 2 is the approval resume that approves
    t1 = trace([span("bev_ops_turn", SID, 1, parent=None), span("recall_decisions", SID, 2), span("suggest_replenishment", SID, 3),
                span("propose_replenishment", SID, 4)], 1)
    t2 = trace([span("bev_ops_turn", SID, 10, parent=None),
                span("approval", SID, 11, outputs={"last_decision": {"status": "approved"}})], 10)
    return [t2, t1]  # out of order on purpose


RECORD = {SID: {"id": "c2-02", "inputs": {"messages": [{"role": "user", "content": "send it"}], "approval": "approve"},
                "outputs": {"response": "Request approved."},
                "expectations": {"expected_route": "uc_function", "acceptable_routes": ["uc_function"],
                                 "expected_tools": ["suggest_replenishment", "create_replenishment_request"], "forbidden_tools": [],
                                 "requires_approval": True}}}


def by_name(results):
    out = {}
    for r in results:
        for fb in (r if isinstance(r, list) else [r]):
            out[fb.name] = fb
    return out


def test_session_trace_orders_and_merges():
    st = SessionTrace(turns())
    assert [s.name for s in st.search_spans()][0] == "bev_ops_turn"
    assert len(st.search_spans()) == 6
    assert session_id_of(turns()) == SID


def test_wrappers_are_session_level_and_score_whole_conversation():
    scorers = get_session_scorers(RECORD, include_judges=False)
    assert all(s.is_session_level_scorer for s in scorers)
    res = by_name([s.run(session=turns()) for s in scorers])
    assert res["tool_recall"].value == 1.0, res["tool_recall"].rationale
    assert res["approval_guard"].value == "yes", res["approval_guard"].rationale
    assert res["route_accuracy"].value == "yes"


def test_unknown_session_is_skipped():
    scorers = get_session_scorers({}, include_judges=False)
    assert all(s.run(session=turns()) == [] for s in scorers)


def test_answer_reads_app_and_eval_shapes():
    from scorers import _answer
    assert _answer({"answer": "a"}) == "a" and _answer({"response": "r"}) == "r" and _answer(None) == ""
    assert json.dumps(_answer("x")) == '"x"'
