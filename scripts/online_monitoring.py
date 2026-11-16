"""Register production monitors (online scorers) on the agent's MLflow experiment. Idempotent: register = upsert, then start.
Scorers need no expectations: live-turn ports of approval_guard/concise, leads_with_answer + cites_sources judges,
MLflow session judges. Judges run on the Gateway llm_fast service (BEV_JUDGE_MODEL overrides).
  .venv/bin/python scripts/online_monitoring.py --dry-run
  .venv/bin/python scripts/online_monitoring.py [--turn-rate 0.2] [--session-rate 0.2] [--deterministic-rate 1.0] [--only a,b]
  .venv/bin/python scripts/online_monitoring.py --list | --stop [--only a,b]
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "setup"))
sys.path.insert(0, str(ROOT / "eval"))
import config as C  # noqa: E402  (loads .env)

# turn-kind tag set by agent/tracing.py; new-message turns only for reply-quality judges
MESSAGE_TURNS = "tags.turn_kind = 'message'"
# offline eval sessions (eval/run_eval.py thread ids eval-<run>-...) share the experiment: not production traffic
NOT_EVAL = "metadata.`mlflow.trace.session` NOT LIKE 'eval-%'"


def _flt(*parts):
    parts = [p for p in parts if p]
    return " AND ".join(parts) or None


def log(msg):
    print(f"[monitor] {msg}", flush=True)


def build_monitors(a):
    # (scorer, sample_rate, filter_string)
    from mlflow.genai.scorers import ConversationalToolCallEfficiency, ConversationCompleteness, Guidelines, UserFrustration
    from online_scorers import approval_guard_live, concise_live
    from scorers import CITE_DATA_RULE, judge_model, leads_judge
    m = a.judge_model or judge_model()
    base = None if a.include_eval else NOT_EVAL
    msg = _flt(MESSAGE_TURNS, base)
    mons = [
        (approval_guard_live, a.deterministic_rate, base),
        (concise_live, a.deterministic_rate, msg),
        (leads_judge(m), a.turn_rate, msg),
        (Guidelines(name="cites_sources", model=m, guidelines=[CITE_DATA_RULE]), a.turn_rate, msg),
        (ConversationCompleteness(model=m), a.session_rate, base),
        # KnowledgeRetention: composed scorer, not serializable as a monitor in MLflow 3.18 (offline eval only)
        (UserFrustration(model=m), a.session_rate, base),
        (ConversationalToolCallEfficiency(model=m), a.session_rate, base),
    ]
    if a.only:
        keep = set(a.only.split(","))
        mons = [x for x in mons if x[0].name in keep]
    return mons, m


def _scorer_configs(experiment_id):
    # raw configs, no deserialization: native scorers (judge model "databricks") need databricks-agents to load
    import inspect
    import json
    from mlflow.genai.scorers import registry as R
    store = next(c for _, c in inspect.getmembers(R, inspect.isclass) if hasattr(c, "_list_current_scorer_configs"))()
    for c in store._list_current_scorer_configs(experiment_id):
        yield json.loads(c.serialized_scorer).get("name"), c.sample_rate, c.filter_string


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--experiment-id", default=os.environ.get("BEV_MLFLOW_EXPERIMENT_ID"))
    p.add_argument("--warehouse-id", default=C.WAREHOUSE_ID)
    p.add_argument("--profile", default=C.PROFILE)
    p.add_argument("--judge-model", default=None, help="default: scorers.judge_model() = databricks:/<cat>.<schema>.llm_fast")
    p.add_argument("--turn-rate", type=float, default=float(os.environ.get("BEV_MONITOR_TURN_RATE", "0.2")))
    p.add_argument("--session-rate", type=float, default=float(os.environ.get("BEV_MONITOR_SESSION_RATE", "0.2")))
    p.add_argument("--deterministic-rate", type=float, default=float(os.environ.get("BEV_MONITOR_DETERMINISTIC_RATE", "1.0")))
    p.add_argument("--only", help="comma-separated scorer names")
    p.add_argument("--include-eval", action="store_true", help="also score offline eval sessions (default: production traffic only)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--list", action="store_true")
    g.add_argument("--stop", action="store_true")
    a = p.parse_args(argv)
    if not a.experiment_id:
        raise SystemExit("[monitor] BEV_MLFLOW_EXPERIMENT_ID is not set")
    for r in (a.turn_rate, a.session_rate, a.deterministic_rate):
        if not 0 < r <= 1:
            raise SystemExit(f"[monitor] sample rates must be in (0, 1], got {r}")

    mons, m = build_monitors(a)
    # dry run: no tracking URI, no network
    if a.dry_run:
        log(f"experiment {a.experiment_id} | warehouse {a.warehouse_id} | judge {m}")
        for s, rate, flt in mons:
            log(f"DRY-RUN  register+start {s.name:34s} rate={rate} filter={flt or '-'}")
        return mons

    import mlflow
    from mlflow.genai.scorers import ScorerSamplingConfig, get_scorer
    mlflow.set_tracking_uri(f"databricks://{a.profile}" if a.profile else "databricks")
    ours = {s.name for s, _, _ in mons}
    if a.list:
        for name, rate, flt in _scorer_configs(a.experiment_id):
            owner = "ours" if name in ours else "native"
            log(f"{name:34s} {owner:6s} rate={rate} filter={flt or '-'}")
        return []
    if a.stop:
        # only stop scorers this script manages; native ones (e.g. issue_monitor_*) are left alone
        live = {name for name, _, _ in _scorer_configs(a.experiment_id)}
        for name in sorted(ours & live):
            get_scorer(name=name, experiment_id=a.experiment_id).stop(experiment_id=a.experiment_id)
            log(f"stopped {name}")
        return []

    # monitoring jobs read UC-backed traces through this warehouse
    from mlflow.tracing import set_databricks_monitoring_sql_warehouse_id
    if a.warehouse_id:
        set_databricks_monitoring_sql_warehouse_id(a.warehouse_id, experiment_id=a.experiment_id)
        log(f"monitoring warehouse = {a.warehouse_id}")
    out, failed = [], []
    for s, rate, flt in mons:
        # one scorer that cannot register must not block the rest
        try:
            reg = s.register(experiment_id=a.experiment_id)
            out.append(reg.start(experiment_id=a.experiment_id, sampling_config=ScorerSamplingConfig(sample_rate=rate, filter_string=flt)))
            log(f"started {s.name:34s} rate={rate} filter={flt or '-'}")
        except Exception as e:  # noqa: BLE001
            failed.append(s.name)
            log(f"WARN  {s.name} not registered: {type(e).__name__}: {str(e)[:160]}")
    if failed:
        raise SystemExit(f"[monitor] {len(failed)} scorer(s) not registered: {', '.join(failed)}")
    return out


if __name__ == "__main__":
    main()
