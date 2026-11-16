"""Trace health for the app's turns: counts bev_ops_turn traces by state over a window and lists the bad ones.
Bad = ERROR, or still IN_PROGRESS after --stuck-min minutes (a worker that never closed its span). Exit 1 if any.
Usage: python3 scripts/trace_health.py [--hours 24] [--stuck-min 10] [--include-eval]
"""
import argparse
import collections
import os
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import mlflow  # noqa: E402

from agent.tracing import init_tracing  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=float(os.environ.get("BEV_TRACE_HEALTH_HOURS", "24")))
    ap.add_argument("--stuck-min", type=float, default=float(os.environ.get("BEV_TRACE_STUCK_MIN", "10")))
    ap.add_argument("--include-eval", action="store_true", help="also count eval sessions (eval-*)")
    ap.add_argument("--max", type=int, default=1000)
    a = ap.parse_args()
    init_tracing()
    now_ms = int(time.time() * 1000)
    since = now_ms - int(a.hours * 3600 * 1000)
    exp = os.environ.get("BEV_MLFLOW_EXPERIMENT_ID") or mlflow.get_experiment_by_name(os.environ["BEV_MLFLOW_EXPERIMENT"]).experiment_id
    traces = mlflow.search_traces(experiment_ids=[exp], filter_string=f"attributes.timestamp_ms > {since}", max_results=a.max,
                                  return_type="list")
    counts, bad = collections.Counter(), []
    for t in traces:
        i = t.info
        md = i.trace_metadata or {}
        session = md.get("mlflow.trace.session") or ""
        if session.startswith("eval-") and not a.include_eval:
            continue
        root = next((s.name for s in t.data.spans if s.parent_id is None), None)
        if root != "bev_ops_turn":
            continue
        state = str(getattr(i.state, "value", i.state))
        counts[state] += 1
        age_min = (now_ms - i.request_time) / 60000
        if state == "ERROR" or (state == "IN_PROGRESS" and age_min > a.stuck_min):
            bad.append((state, i.trace_id, session, round(age_min), (i.execution_duration or 0) / 1000, str(i.request_preview)[:80]))
    print(f"bev_ops_turn traces, last {a.hours:g} h: {dict(counts) or 'none'}")
    for b in sorted(bad, key=lambda b: b[3]):
        print(f"  {b[0]:<11} {b[3]:>5} min ago  {b[4]:6.1f}s  {b[1]}  session {b[2]}  {b[5]!r}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
