"""Ask each benchmark question to a Genie Agent and compare its first result row with the expected SQL.

Usage: python run_benchmarks.py <config.yaml> <space_id>
"""
import concurrent.futures as cf
import os
import sys
import time

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))
from build_space import sub  # noqa: E402
from sqlrun import client, run  # noqa: E402


def norm(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return str(v).strip().lower()


def ask(w, space_id, q, timeout=180):
    msg = w.genie.start_conversation(space_id, q).result(timeout=__import__("datetime").timedelta(seconds=timeout))
    sql, rows = None, None
    for a in msg.attachments or []:
        if a.query:
            sql = a.query.query
            res = w.genie.get_message_attachment_query_result(space_id, msg.conversation_id, msg.message_id, a.attachment_id)
            rows = res.statement_response.result.data_array if res.statement_response.result else []
    return msg.status.value if msg.status else None, sql, rows


def check(w, space_id, b):
    t0 = time.time()
    expected = run(sub(b["sql"]).strip(), w) or [[None]]
    try:
        status, sql, rows = ask(w, space_id, b["question"])
    except Exception as ex:  # noqa: BLE001
        return b["question"], False, f"error {ex}", time.time() - t0
    got = rows[0] if rows else [None]
    exp_vals = {norm(x) for x in expected[0]}
    ok = bool(exp_vals & {norm(x) for x in got})
    return b["question"], ok, f"exp={expected[0][:3]} got={got[:4]}", time.time() - t0


def main(cfg_path, space_id):
    cfg = yaml.safe_load(open(cfg_path))
    w = client()
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda b: check(w, space_id, b), cfg["benchmarks"]))
    passed = sum(r[1] for r in results)
    for q, ok, detail, secs in results:
        print(f"{'PASS' if ok else 'FAIL'} {secs:5.0f}s  {q[:70]}\n         {detail[:200]}")
    print(f"\n{passed}/{len(results)} passed")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
