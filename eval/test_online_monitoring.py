"""Offline tests for online monitoring (no network). Run: .venv/bin/python -m pytest -q eval/test_online_monitoring.py"""
import os
import sys
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(__file__)
sys.path[:0] = [HERE, os.path.join(HERE, "..", "setup"), os.path.join(HERE, "..", "scripts")]
os.environ.setdefault("BEV_MLFLOW_EXPERIMENT_ID", "123")

import online_monitoring as om  # noqa: E402
from mlflow.genai.scorers import Scorer  # noqa: E402
from online_scorers import approval_guard_live, concise_live  # noqa: E402
from scorers import get_scorers  # noqa: E402


def span(name, start, outputs=None):
    return SimpleNamespace(name=name, span_id=f"{name}-{start}", parent_id="root", start_time_ns=start, outputs=outputs, inputs=None,
                           attributes={})


def trace(spans):
    return SimpleNamespace(search_spans=lambda **kw: spans)


APPROVED = trace([span("bev_ops_turn", 1), span("approval", 2, outputs={"last_decision": {"status": "approved"}})])
PROPOSAL = trace([span("bev_ops_turn", 1), span("propose_replenishment", 2), span("approval", 3)])


def test_approval_guard_live():
    assert approval_guard_live(inputs={"decision": "approve", "reason": ""}, trace=APPROVED).value == "yes"
    assert approval_guard_live(inputs={"message": "approve it all"}, trace=APPROVED).value == "no"
    assert approval_guard_live(inputs={"message": "top up TX"}, trace=PROPOSAL).value == "yes"


def test_concise_parity_with_offline():
    offline = {s.name: s for s in get_scorers(include_judges=True) if s.name == "concise"}["concise"]
    for text in ["**7 positions at risk in Texas.** Lowest is FL-D2.", "word " * 200, "I called oos_risk_skus for you.",
                 "| a | b |\n|---|---|\n" + "| x | y |\n" * 100 + "Short."]:
        out = {"answer": text}
        assert concise_live(outputs=out).value == offline(inputs={}, outputs=out).value, text[:30]


def test_concise_live_reads_json_string_outputs():
    # live traces: outputs arrive as a JSON string; the envelope must not count as a leak
    import json
    assert concise_live(outputs=json.dumps({"answer": "**California** leads at $481,167."})).value == "yes"
    assert concise_live(outputs=json.dumps({"answer": "I called requests_summary."})).value == "no"


def test_serialized_bodies_run_standalone():
    # what the monitoring job does: rebuild from the stored body only
    with mock.patch("mlflow.genai.scorers.base.is_databricks_uri", return_value=True), \
         mock.patch("mlflow.genai.scorers.base._should_reconstruct_scorer_code", return_value=True):
        for s in (approval_guard_live, concise_live):
            Scorer.model_validate(s.model_dump())
        g = Scorer.model_validate(approval_guard_live.model_dump())
        c = Scorer.model_validate(concise_live.model_dump())
    assert g(inputs={"message": "hi"}, trace=APPROVED).value == "no"
    assert c(outputs={"answer": "Short answer."}).value == "yes"


def test_dry_run_lists_all_without_network():
    with mock.patch("mlflow.set_tracking_uri") as uri:
        mons = om.main(["--dry-run", "--turn-rate", "0.5"])
    uri.assert_not_called()
    names = [s.name for s, _, _ in mons]
    assert names == ["approval_guard_live", "concise_live", "leads_with_answer", "cites_sources", "conversation_completeness",
                     "user_frustration", "conversational_tool_call_efficiency"]
    assert all((getattr(s, "model", None) or "databricks").startswith("databricks") for s, _, _ in mons)
    assert dict((s.name, r) for s, r, _ in mons)["leads_with_answer"] == 0.5
    assert "bev_ops_rules" not in names  # parked


def test_register_path_mocked():
    registered = []

    def fake_register(self, *, name=None, experiment_id=None):
        registered.append((self.name, experiment_id))
        return self

    started = []

    def fake_start(self, *, name=None, experiment_id=None, sampling_config):
        started.append((self.name, sampling_config.sample_rate, sampling_config.filter_string))
        return self

    with mock.patch("mlflow.set_tracking_uri"), \
         mock.patch("mlflow.tracing.set_databricks_monitoring_sql_warehouse_id") as wh, \
         mock.patch.object(Scorer, "register", fake_register), mock.patch.object(Scorer, "start", fake_start):
        om.main(["--experiment-id", "999", "--warehouse-id", "wh1", "--only", "approval_guard_live,leads_with_answer,user_frustration"])
    wh.assert_called_once_with("wh1", experiment_id="999")
    assert registered == [("approval_guard_live", "999"), ("leads_with_answer", "999"), ("user_frustration", "999")]
    # production traffic only by default: eval sessions excluded on every monitor
    msg = f"{om.MESSAGE_TURNS} AND {om.NOT_EVAL}"
    assert started == [("approval_guard_live", 1.0, om.NOT_EVAL), ("leads_with_answer", 0.2, msg), ("user_frustration", 0.2, om.NOT_EVAL)]


def test_include_eval_drops_the_exclusion():
    mons, _ = om.build_monitors(om.argparse.Namespace(judge_model="databricks:/x", include_eval=True, only=None, turn_rate=0.2,
                                                     session_rate=0.2, deterministic_rate=1.0))
    assert {m[0].name: m[2] for m in mons}["approval_guard_live"] is None


def test_bad_rate_rejected():
    try:
        om.main(["--dry-run", "--session-rate", "0"])
    except SystemExit as e:
        assert "sample rates" in str(e)
    else:
        raise AssertionError("rate 0 accepted")


def test_stop_leaves_native_scorers_alone():
    # native issue monitors live next to ours; --stop must only touch ours
    live = [("approval_guard_live", 1.0, None), ("issue_monitor_abc", 1.0, None)]
    stopped = []
    fake = lambda name, experiment_id: SimpleNamespace(stop=lambda experiment_id: stopped.append(name))  # noqa: E731
    with mock.patch.object(om, "_scorer_configs", return_value=live), \
         mock.patch("mlflow.set_tracking_uri"), mock.patch("mlflow.genai.scorers.get_scorer", side_effect=fake):
        om.main(["--stop"])
    assert stopped == ["approval_guard_live"]
