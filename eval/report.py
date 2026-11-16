"""Per-record readout of an eval run: turns, scorer values + rationale for failures, final answer.
Works for both run shapes: sessions (one trace per turn, assessments on the session's first trace) and legacy (one trace per record).
Usage: python eval/report.py <run_id> [--answers]
"""
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "setup"))
import config as C  # noqa: E402

PASS = {"yes", True, 1.0, "none"}  # "none" = UserFrustration's best value


def _root_out(trace):
    out = next(s for s in trace.search_spans() if s.parent_id is None).outputs
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            pass
    return out.get("response", out.get("answer", out)) if isinstance(out, dict) else out


def _print_assessments(fb):
    bad = [a for a in fb if a.feedback.value not in PASS]
    for a in bad:
        if a.feedback.value is None:
            print(f"   ERR  {a.name} | {str(getattr(a.feedback.error, 'error_message', a.feedback.error))[:240]}")
        else:
            print(f"   FAIL {a.name}={a.feedback.value} | {(a.rationale or '')[:240]}")
    return len(fb) - len(bad), len(fb)


def main(run_id, answers=False):
    import mlflow
    os.environ.setdefault("MLFLOW_TRACING_SQL_WAREHOUSE_ID", os.environ.get("BEV_WAREHOUSE_ID", ""))
    mlflow.set_tracking_uri(f"databricks://{C.PROFILE}")
    exp = os.environ["BEV_MLFLOW_EXPERIMENT_ID"]
    run = mlflow.get_run(run_id)
    if run.data.tags.get("eval_mode") == "sessions":
        # sessions: group turn traces by session; setup sessions (~setup) are context only
        prefix = f"eval-{run.data.tags['eval_run']}-"
        traces = mlflow.search_traces(locations=[exp], filter_string=f"metadata.`mlflow.trace.session` LIKE '{prefix}%'",
                                      return_type="list", max_results=5000)
        by_s = defaultdict(list)
        for t in traces:
            sid = t.info.trace_metadata.get("mlflow.trace.session", "")
            if "~setup" not in sid:
                by_s[sid].append(t)
        for sid, ts in sorted(by_s.items(), key=lambda kv: kv[1][0].info.tags.get("record_id") or kv[0]):
            ts.sort(key=lambda t: t.info.request_time or 0)
            rid = next((t.info.tags.get("record_id") for t in ts if t.info.tags.get("record_id")), sid)
            fb = [a for t in ts for a in (t.info.assessments or []) if a.feedback]
            print(f"{rid}: {len(ts)} turn(s)")
            ok, n = _print_assessments(fb) if fb else (0, 0)
            print(f"   {ok}/{n} pass")
            if answers:
                for i, t in enumerate(ts, 1):
                    print(f"   t{i} ->", str(_root_out(t))[:300].replace("\n", " "))
        return
    traces = mlflow.search_traces(run_id=run_id, return_type="list", locations=[exp])
    for t in sorted(traces, key=lambda t: t.info.tags.get("record_id") or ""):
        spans = t.search_spans()
        turns = sum(1 for s in spans if s.name == "bev_ops_turn")
        fb = [a for a in t.info.assessments if a.feedback]
        print(f"{t.info.tags.get('record_id')}: {turns} turn(s), {len(spans)} spans")
        ok, n = _print_assessments(fb)
        print(f"   {ok}/{n} pass")
        if answers:
            print("   ->", str(_root_out(t))[:500].replace("\n", " "))


if __name__ == "__main__":
    main(sys.argv[1], "--answers" in sys.argv)
