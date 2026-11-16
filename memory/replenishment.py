"""HITL replenishment requests on Lakebase (agent_ops.replenishment_requests): propose -> approve | reject, with decision memory.
Identity comes from the caller (user email from the forwarded token), never from the LLM. All SQL is parameterized.
In the app the connection is the app SP's (writes happen only after the app checked the caller's group); four-eyes and the
manager threshold are enforced here too, as defense in depth.
"""
import os

from psycopg.rows import dict_row

from memory.decisions import save_decision, summarize

TABLE = "agent_ops.replenishment_requests"
STATUSES = ("proposed", "approved", "rejected", "withdrawn")
RETURNING = "request_id, created_at, status, requested_by, market_id, distributor_id, sku, qty_cases, reason, amended_at, decided_at, decision_reason"
MAX_QTY_CASES = 20000  # sanity cap
MANAGER_THRESHOLD_CASES = float(os.environ.get("BEV_MANAGER_THRESHOLD_CASES", "500"))  # policy: above this, a manager decides
# a requester's open (proposed) requests at any time; bounds what a prompt or an injected document can queue
MAX_PENDING_PER_USER = int(os.environ.get("BEV_MAX_PENDING_PER_USER", "10"))


class ProposalLimit(ValueError):
    """The requester already has too many open requests."""


def needs_manager(qty_cases) -> bool:
    return float(qty_cases) > MANAGER_THRESHOLD_CASES


def _lookup(cur, distributor_id, sku):
    cur.execute("SELECT distributor_id, distributor_name, market_id FROM agent_ops.ref_distributor WHERE distributor_id = %s", (distributor_id,))
    dist = cur.fetchone()
    cur.execute("SELECT sku, product_name FROM agent_ops.ref_product WHERE sku = %s", (sku,))
    prod = cur.fetchone()
    if not dist:
        raise ValueError(f"unknown distributor_id {distributor_id!r}")
    if not prod:
        raise ValueError(f"unknown sku {sku!r} (pass the SKU code, e.g. HB-LAGR-355B)")
    return dist, prod


def propose_request(conn, requested_by, distributor_id, sku, qty_cases, reason, market_id=None, thread_id=None, trace_id=None):
    if not requested_by:
        raise ValueError("requested_by is required")
    qty = float(qty_cases)
    if not 0 < qty <= MAX_QTY_CASES:
        raise ValueError(f"qty_cases must be > 0 and <= {MAX_QTY_CASES}")
    if not reason or not reason.strip():
        raise ValueError("reason is required")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        dist, prod = _lookup(cur, distributor_id, sku)
        if market_id and market_id != dist["market_id"]:
            raise ValueError(f"{distributor_id} belongs to market {dist['market_id']}, not {market_id}")
        # one open request per requester x distributor x SKU: a repeat returns the existing one instead of a duplicate.
        # the per-requester transaction lock makes the check + cap + insert atomic across concurrent turns
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("replenishment:" + requested_by.lower(),))
        cur.execute(f"""SELECT {RETURNING} FROM {TABLE}
                        WHERE lower(requested_by) = lower(%s) AND distributor_id = %s AND sku = %s AND status = 'proposed'
                        ORDER BY created_at DESC LIMIT 1""", (requested_by, distributor_id, sku))
        existing = cur.fetchone()
        if existing:
            return existing | {"distributor_name": dist["distributor_name"], "product_name": prod["product_name"],
                               "needs_manager_approval": needs_manager(existing["qty_cases"]), "duplicate": True}
        cur.execute(f"SELECT count(*) AS n FROM {TABLE} WHERE lower(requested_by) = lower(%s) AND status = 'proposed'", (requested_by,))
        if cur.fetchone()["n"] >= MAX_PENDING_PER_USER:
            raise ProposalLimit(f"you already have {MAX_PENDING_PER_USER} open requests: wait for decisions or withdraw some first")
        cur.execute(f"""INSERT INTO {TABLE} (requested_by, market_id, distributor_id, sku, qty_cases, reason, thread_id, trace_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        RETURNING request_id, created_at, status, requested_by, market_id, distributor_id, sku, qty_cases, reason""",
                    (requested_by, dist["market_id"], distributor_id, sku, qty, reason.strip(), thread_id, trace_id))
        row = cur.fetchone()
    return row | {"distributor_name": dist["distributor_name"], "product_name": prod["product_name"],
                  "needs_manager_approval": needs_manager(qty), "duplicate": False}


def get_request(conn, request_id):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT request_id, requested_by, status, qty_cases, market_id, distributor_id, sku FROM {TABLE} WHERE request_id = %s",
                    (request_id,))
        return cur.fetchone()


def _decide(conn, request_id, decided_by, decision_reason, action, embed_fn, allow_self_approval, manager_ok):
    if not decided_by:
        raise ValueError("decided_by is required")
    if not decision_reason or not decision_reason.strip():
        raise ValueError("decision_reason is required")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT requested_by, qty_cases FROM {TABLE} WHERE request_id = %s AND status = 'proposed' FOR UPDATE", (request_id,))
        cur_row = cur.fetchone()
        if not cur_row:
            raise ValueError(f"request {request_id} not found or not in status 'proposed'")
        if not allow_self_approval and (cur_row["requested_by"] or "").lower() == (decided_by or "").lower():
            raise PermissionError("requester cannot decide their own request")
        if action == "approved" and needs_manager(cur_row["qty_cases"]) and not manager_ok:
            raise PermissionError(f"above {MANAGER_THRESHOLD_CASES:g} cases a manager must approve")
        cur.execute(f"""UPDATE {TABLE} SET status = %s, decided_by = %s, decided_at = now(), decision_reason = %s
                        WHERE request_id = %s AND status = 'proposed'
                        RETURNING request_id, status, market_id, distributor_id, sku, qty_cases, reason, decided_by, decided_at, decision_reason""",
                    (action, decided_by, decision_reason.strip(), request_id))
        row = cur.fetchone()
        dist, prod = _lookup(cur, row["distributor_id"], row["sku"])
        summary = summarize(action, float(row["qty_cases"]), prod["product_name"], row["sku"], row["distributor_id"],
                            dist["distributor_name"], row["market_id"], row["decision_reason"], row["reason"])
        dec = save_decision(conn, decided_by, action, row["decision_reason"], summary, market_id=row["market_id"],
                            distributor_id=row["distributor_id"], sku=row["sku"], qty_cases=float(row["qty_cases"]),
                            request_id=row["request_id"], embed_fn=embed_fn)
    return row | {"decision_id": dec["decision_id"], "summary": summary}


def approve_request(conn, request_id, decided_by, decision_reason, embed_fn=None, allow_self_approval=False, manager_ok=False):
    return _decide(conn, request_id, decided_by, decision_reason, "approved", embed_fn, allow_self_approval, manager_ok)


def reject_request(conn, request_id, decided_by, decision_reason, embed_fn=None, allow_self_approval=False, manager_ok=True):
    # rejecting never needs a manager
    return _decide(conn, request_id, decided_by, decision_reason, "rejected", embed_fn, allow_self_approval, manager_ok)


def list_requests(conn, status=None, requested_by=None, distributor_id=None, sku=None, limit=20, market_id=None):
    if status and status not in STATUSES:
        raise ValueError("status must be one of " + ", ".join(STATUSES))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""SELECT request_id, created_at, requested_by, market_id, distributor_id, sku, qty_cases, reason,
                               status, decided_by, decided_at, decision_reason
                        FROM {TABLE}
                        WHERE (%s::text IS NULL OR status = %s) AND (%s::text IS NULL OR requested_by = %s)
                          AND (%s::text IS NULL OR distributor_id = %s) AND (%s::text IS NULL OR sku = %s)
                          AND (%s::text IS NULL OR market_id = %s)
                        ORDER BY created_at DESC LIMIT %s""",
                    (status, status, requested_by, requested_by, distributor_id, distributor_id, sku, sku, market_id, market_id,
                     min(int(limit), 100)))
        return cur.fetchall()


def summarize_requests(conn, requested_by=None, market_id=None, since_days=None):
    # complete ledger counts (every status), per status x distributor; requested_by None = whole team
    with conn.cursor(row_factory=dict_row) as cur:
        # items carry the product name: users read names, not SKU codes
        cur.execute(f"""SELECT r.status, r.distributor_id, d.distributor_name, r.market_id, count(*) AS requests, sum(r.qty_cases) AS cases,
                               string_agg(coalesce(p.product_name, r.sku) || ' (' || r.sku || '): ' || r.qty_cases::int || ' cases', '; '
                                          ORDER BY r.created_at DESC) AS items,
                               max(coalesce(r.decided_at, r.created_at)) AS last_change
                        FROM {TABLE} r
                        LEFT JOIN agent_ops.ref_product p ON p.sku = r.sku
                        LEFT JOIN agent_ops.ref_distributor d ON d.distributor_id = r.distributor_id
                        WHERE (%s::text IS NULL OR r.requested_by = %s) AND (%s::text IS NULL OR r.market_id = %s)
                          AND (%s::int IS NULL OR r.created_at >= now() - make_interval(days => %s::int))
                        GROUP BY r.status, r.distributor_id, d.distributor_name, r.market_id
                        ORDER BY r.status, cases DESC""",
                    (requested_by, requested_by, market_id, market_id, since_days, since_days))
        rows = cur.fetchall()
    totals = {}
    for r in rows:
        t = totals.setdefault(r["status"], {"requests": 0, "cases": 0.0, "distributors": 0})
        t["requests"] += r["requests"]
        t["cases"] += float(r["cases"] or 0)
        t["distributors"] += 1
    return {"totals": totals, "by_distributor": rows}


def _own_proposed(cur, request_id, requested_by):
    # only the requester, only while nobody has decided it
    cur.execute(f"SELECT requested_by, status FROM {TABLE} WHERE request_id = %s FOR UPDATE", (request_id,))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"request {request_id} not found")
    if (row["requested_by"] or "").lower() != (requested_by or "").lower():
        raise PermissionError("only the requester can change this request")
    if row["status"] != "proposed":
        raise ValueError(f"request {request_id} is already {row['status']}")


def amend_request(conn, request_id, requested_by, qty_cases=None, reason=None):
    if qty_cases is None and not (reason or "").strip():
        raise ValueError("nothing to change: pass qty_cases and/or reason")
    if qty_cases is not None and not 0 < float(qty_cases) <= MAX_QTY_CASES:
        raise ValueError(f"qty_cases must be > 0 and <= {MAX_QTY_CASES}")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        _own_proposed(cur, request_id, requested_by)
        cur.execute(f"""UPDATE {TABLE} SET qty_cases = coalesce(%s, qty_cases), reason = coalesce(%s, reason), amended_at = now()
                        WHERE request_id = %s AND status = 'proposed' RETURNING {RETURNING}""",
                    (None if qty_cases is None else float(qty_cases), (reason or "").strip() or None, request_id))
        row = cur.fetchone()
    return row | {"needs_manager_approval": needs_manager(row["qty_cases"])}


def withdraw_request(conn, request_id, requested_by, reason):
    if not (reason or "").strip():
        raise ValueError("reason is required")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        _own_proposed(cur, request_id, requested_by)
        cur.execute(f"""UPDATE {TABLE} SET status = 'withdrawn', decided_by = %s, decided_at = now(), decision_reason = %s
                        WHERE request_id = %s AND status = 'proposed' RETURNING {RETURNING}""",
                    (requested_by, reason.strip(), request_id))
        return cur.fetchone()
