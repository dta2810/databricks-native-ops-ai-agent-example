"""Run before a demo: move the whole synthetic world by whole weeks so the latest data week is the last completed week.
Numbers never change, only dates (see setup/demo_clock.py).
  1. UC: in-place UPDATE of every date column (keeps comments, PK/FK, grants). Per-table shift is derived from that table's
     own max date vs canonical, so a re-run after a failure converges (no double shift).
  2. Lakebase: corpus re-rendered + re-embedded (41), seed decisions re-created as "yesterday" (51).
     --reset-demo also deletes requests/decisions created during previous demos (thread_id <> 'seed').
  3. Checks: hero numbers + both Genie benchmark suites (--skip-checks to skip the ~4 min benchmarks).
  Lakebase is always reloaded (unless --skip-lakebase): it is derived from the canonical files + the live delta, so a re-run
  after a failure past the UC step converges too.
Usage: python3 scripts/refresh_dates.py [--anchor YYYY-MM-DD] [--reset-demo] [--skip-lakebase] [--skip-checks] [--dry-run]
"""
import argparse
import importlib
import os
import subprocess
import sys
from datetime import date, timedelta

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "setup"))
sys.path.insert(0, ROOT)
import config as C  # noqa: E402
from demo_clock import CANONICAL_END_WEEK, anchor_for, current_delta_days  # noqa: E402
from sqlrun import client, run  # noqa: E402

FQ = f"{C.CATALOG}.{C.SCHEMA}"
# table -> (column that defines its shift, canonical max of that column, all date/timestamp columns to move)
UC_TABLES = {
    "fact_depletions": ("week_start", CANONICAL_END_WEEK, ["week_start"]),
    "fact_shipments": ("week_start", CANONICAL_END_WEEK, ["week_start"]),
    "fact_inventory": ("snapshot_date", CANONICAL_END_WEEK + timedelta(days=6), ["snapshot_date"]),
    "fact_promotions": ("end_date", CANONICAL_END_WEEK + timedelta(weeks=3, days=6), ["start_date", "end_date"]),
}
# fact_depletions last: it is the delta source the loaders read
ORDER = ["fact_shipments", "fact_inventory", "fact_promotions", "fact_depletions"]
GENIE = [("genie/commercial_promotions.yaml", "BEV_GENIE_COMMERCIAL"),
         ("genie/supply_replenishment/supply_replenishment.yaml", "BEV_GENIE_SUPPLY")]


def log(msg):
    print(f"[refresh] {msg}", flush=True)


def shift_uc(w, target, dry):
    for t in ORDER:
        key, canon, cols = UC_TABLES[t]
        mx = run(f"SELECT max({key}) FROM {FQ}.{t}", w)[0][0]
        step = (target - (date.fromisoformat(str(mx)[:10]) - canon).days)
        if step == 0:
            log(f"{t}: already at target")
            continue
        sets = ", ".join(f"{c} = date_add({c}, {step})" for c in cols)
        sql = f"UPDATE {FQ}.{t} SET {sets}"
        log(f"{t}: shift {step:+d} days" + (" (dry run)" if dry else ""))
        if not dry:
            run(sql, w)
    # UC request mirror keeps real timestamps (when the demo happened)
    n = int(run(f"SELECT count(*) FROM {FQ}.replenishment_requests", w)[0][0])
    if n:
        log(f"replenishment_requests (UC): {n} rows kept as-is (real timestamps)")


def reset_demo(dry):
    from lakebase_conn import connect
    sqls = ["DELETE FROM agent_mem.decision_log WHERE request_id IN (SELECT request_id FROM agent_ops.replenishment_requests WHERE thread_id <> 'seed')",
            "DELETE FROM agent_ops.replenishment_requests WHERE thread_id <> 'seed'"]
    with connect() as conn:
        n = conn.execute("SELECT count(*) FROM agent_ops.replenishment_requests WHERE thread_id <> 'seed'").fetchone()[0]
        log(f"reset demo: {n} non-seed requests (+ their decisions)" + (" (dry run)" if dry else " deleted"))
        if not dry and n:
            with conn.transaction():
                for s in sqls:
                    conn.execute(s)


def checks(w, skip_benchmarks):
    hero = {
        "latest week": f"SELECT max(week_start) FROM {FQ}.fact_depletions",
        "at-risk positions (45)": f"SELECT count(*) FROM {FQ}.v_stock_cover WHERE status = 'AT RISK'",
        "PR-0422 CA-D1 uplift (-0.047)": f"SELECT uplift_pct FROM {FQ}.promo_by_distributor('PR-0422') WHERE distributor_id = 'CA-D1'",
        "active promotions (1)": f"SELECT count(*) FROM {FQ}.fact_promotions WHERE status = 'Active'",
    }
    for k, q in hero.items():
        log(f"check {k}: {run(q, w)[0][0]}")
    if skip_benchmarks:
        return True
    procs = [subprocess.Popen([sys.executable, "genie/run_benchmarks.py", cfg, os.environ[env]], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for cfg, env in GENIE]
    ok = True
    for (cfg, _), p in zip(GENIE, procs):
        out = p.communicate()[0].strip().splitlines()
        fails = [l for l in out if l.startswith("FAIL")]
        log(f"benchmarks {os.path.basename(cfg)}: {out[-1] if out else 'no output'}")
        for l in fails:
            log(f"  {l}")
        ok = ok and p.returncode == 0 and not fails
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchor", type=date.fromisoformat, help="latest data week (a Monday); default = last completed week")
    ap.add_argument("--reset-demo", action="store_true")
    ap.add_argument("--skip-checks", action="store_true", help="skip the Genie benchmarks (hero checks still run)")
    ap.add_argument("--skip-lakebase", action="store_true", help="do not reload the corpus and seed decisions")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    anchor = a.anchor or anchor_for()
    if anchor.weekday() != 0:
        raise SystemExit(f"anchor {anchor} is not a Monday")
    target = (anchor - CANONICAL_END_WEEK).days
    w = client()
    before = current_delta_days(run, w)
    log(f"anchor {anchor} | target delta {target:+d} days | current {before:+d}")
    shift_uc(w, target, a.dry_run)
    if a.reset_demo:
        reset_demo(a.dry_run)
    if a.dry_run:
        log("dry run: Lakebase corpus/seeds not reloaded")
        return
    if a.skip_lakebase:
        log("--skip-lakebase: corpus + seeds not reloaded")
    else:
        importlib.import_module("41_load_corpus").main()
        importlib.import_module("51_seed_memory").main()
    ok = checks(w, a.skip_checks)
    log("READY for the demo" if ok else "CHECKS FAILED: see above before demoing")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
