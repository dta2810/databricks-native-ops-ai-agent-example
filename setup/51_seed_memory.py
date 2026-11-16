"""Seed demo decisions through the real HITL path (propose -> reject/approve), backdated one day, then rebuild decision_log indexes.
Idempotent: removes previous seed rows (thread_id = 'seed'). Usage: python3 setup/51_seed_memory.py
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, ROOT)
import config as C
from lakebase_conn import connect
from memory.replenishment import approve_request, propose_request, reject_request
from rag.search import embed
from demo_clock import current_delta_days, shift_text
from sqlrun import client, run

SEED = [
    # (distributor, sku, qty, request reason, action, decision reason)
    ("FL-D2", "HB-LAGR-355B", 600, "Stock cover below 3 days at FL-D2 for Harbor Lager 355ml Bottle", "rejected",
     "Distributor on credit hold (contract DA-FL-D2-2026): no top-ups until the hold is lifted; cover convenience stores via FL-D1/FL-D3"),
    ("TX-D1", "VT-ORIG-473C", 450, "Volt Original at ~2.4 days of cover in TX-D1", "approved",
     "Approved within can-line allocation; ship from the next TX allocation slot"),
]


def main():
    w = client()
    user = os.environ.get("BEV_SEED_USER") or w.current_user.me().user_name
    emb = lambda texts: embed(texts, w.config.host, w.config.authenticate(), C.EMBEDDING_SERVICE)
    delta = current_delta_days(run, w)  # doc ids in reasons follow the demo clock
    with connect() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM agent_mem.decision_log WHERE request_id IN (SELECT request_id FROM agent_ops.replenishment_requests WHERE thread_id = 'seed')")
            conn.execute("DELETE FROM agent_ops.replenishment_requests WHERE thread_id = 'seed'")
        for dist, sku, qty, why, action, decision in SEED:
            why, decision = shift_text(why, delta), shift_text(decision, delta)
            req = propose_request(conn, user, dist, sku, qty, why, thread_id="seed")
            fn = reject_request if action == "rejected" else approve_request
            # seed = admin path (owner connection): one identity plays requester and decider
            out = fn(conn, req["request_id"], user, decision, embed_fn=emb, allow_self_approval=True, manager_ok=True)
            print(out["status"], out["request_id"], "|", out["summary"][:110])
        # backdate so the demo reads as "yesterday's decision"
        with conn.transaction():
            conn.execute("""UPDATE agent_ops.replenishment_requests SET created_at = created_at - interval '1 day',
                            decided_at = decided_at - interval '1 day' WHERE thread_id = 'seed'""")
            conn.execute("""UPDATE agent_mem.decision_log SET created_at = created_at - interval '1 day'
                            WHERE request_id IN (SELECT request_id FROM agent_ops.replenishment_requests WHERE thread_id = 'seed')""")
    import importlib
    importlib.import_module("50_lakebase_memory").main(indexes=True)


if __name__ == "__main__":
    main()
