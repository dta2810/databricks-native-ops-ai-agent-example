"""Local tests against Lakebase as the CLI user: recall of seeded decisions, HITL guards, post-index insert searchable. Cleans up its rows.
Usage: python3 memory/test_memory_local.py
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "setup"))
import config as C
from lakebase_conn import connect
from memory.decisions import recall_decisions
from memory.replenishment import approve_request, list_requests, propose_request, reject_request
from rag.search import embed
from demo_clock import current_delta_days, shift_text
from sqlrun import client, run

ok = 0


def check(name, cond, detail=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""))
    ok += bool(cond)
    return cond


def raises(fn, exc=Exception):
    try:
        fn()
    except exc as e:
        return str(e)[:90]
    return None


w = client()
user = w.current_user.me().user_name
emb = lambda t: embed(t, w.config.host, w.config.authenticate(), C.EMBEDDING_SERVICE)
with connect() as conn:
    # recall: next-day question, hybrid
    r = recall_decisions(conn, user, "Should we top up Harbor Lager at FL-D2 again?", embed_fn=emb)
    check("hybrid recall top1 = FL-D2 credit-hold rejection", r and r[0]["distributor_id"] == "FL-D2" and r[0]["action"] == "rejected", r[0]["summary"][:80] if r else "")
    r = recall_decisions(conn, user, "credit hold", market_id="FL", embed_fn=emb)
    check("filtered recall market=FL", len(r) == 1 and r[0]["sku"] == "HB-LAGR-355B")
    r = recall_decisions(conn, user, shift_text("DA-FL-D2-2026", current_delta_days(run)))
    check("BM25-only recall by contract id (no embed_fn)", r and r[0]["distributor_id"] == "FL-D2")
    r = recall_decisions(conn, user, sku="VT-ORIG-473C")
    check("no-query recall by entity", r and r[0]["action"] == "approved")
    check("user isolation", recall_decisions(conn, "someone.else@example.com", "Harbor Lager") == [])

    # guards
    check("unknown sku rejected", raises(lambda: propose_request(conn, user, "FL-D2", "Harbor Lager", 10, "x")))
    check("market mismatch rejected", raises(lambda: propose_request(conn, user, "FL-D2", "HB-LAGR-355B", 10, "x", market_id="CA")))
    check("qty <= 0 rejected", raises(lambda: propose_request(conn, user, "FL-D2", "HB-LAGR-355B", 0, "x")))
    req = propose_request(conn, user, "CA-D2", "SG-ORNG-1000T", 120, "TEST cover 6 days during PR-0422", thread_id="test")
    check("propose -> proposed", req["status"] == "proposed" and req["market_id"] == "CA")
    check("self-approval blocked when disabled", raises(lambda: approve_request(conn, req["request_id"], user, "ok", allow_self_approval=False), PermissionError))
    out = approve_request(conn, req["request_id"], user, "TEST approved, promo lift at CA-D2 confirmed", embed_fn=emb, allow_self_approval=True, manager_ok=True)
    check("approve -> approved + decision row", out["status"] == "approved" and out["decision_id"])
    check("double decision blocked", raises(lambda: reject_request(conn, req["request_id"], user, "late", allow_self_approval=True)))
    check("list_requests filter", any(x["request_id"] == req["request_id"] for x in list_requests(conn, status="approved", sku="SG-ORNG-1000T")))
    r = recall_decisions(conn, user, "Sunny Grove Orange promo lift CA-D2", embed_fn=emb)
    check("row inserted after index build is recalled", r and r[0]["sku"] == "SG-ORNG-1000T", f"arms via score {r[0]['score']:.4f}" if r else "")
    r = recall_decisions(conn, user, "PR-0422")
    check("post-index row found by BM25 arm", r and r[0]["sku"] == "SG-ORNG-1000T")

    # cleanup test rows
    with conn.transaction():
        conn.execute("DELETE FROM agent_mem.decision_log WHERE request_id IN (SELECT request_id FROM agent_ops.replenishment_requests WHERE thread_id = 'test')")
        conn.execute("DELETE FROM agent_ops.replenishment_requests WHERE thread_id = 'test'")
    plan = conn.execute("EXPLAIN SELECT decision_id FROM agent_mem.decision_log ORDER BY embedding <=> (SELECT embedding FROM agent_mem.decision_log LIMIT 1) LIMIT 3").fetchall()
    print("ANN plan:", " / ".join(p[0].strip() for p in plan if "Scan" in p[0]))
print(f"{ok} checks passed")
