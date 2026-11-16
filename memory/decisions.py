"""Decision memory on Lakebase (agent_mem.decision_log): save approved/rejected decisions, recall them by entity + hybrid search.
Team-shared on purpose: with four-eyes approval the decider is not the requester, so a per-user filter would hide the approver's
decision from the planner who asked. user_id is kept as the author (decided_by). Writes run as the app SP (after the app checks the
approver group); group roles only read.
Caller provides the psycopg connection and an optional embed_fn(texts) -> vectors.
"""
from psycopg.rows import dict_row

from rag.search import RRF_K, vec_literal

TABLE = "agent_mem.decision_log"
BM25_INDEX = "agent_mem.decision_log_bm25"


def summarize(action, qty_cases, product_name, sku, distributor_id, distributor_name, market_id, reason, request_reason=None):
    verb = "Approved" if action == "approved" else "Rejected"
    qty = f"{qty_cases:g} cases of " if qty_cases else ""
    return f"{verb} replenishment of {qty}{product_name} ({sku}) for {distributor_id} {distributor_name} in {market_id}. Reason: {reason}" + (f" Request context: {request_reason}" if request_reason else "")


def save_decision(conn, user_id, action, reason, summary, market_id=None, distributor_id=None, sku=None,
                  qty_cases=None, request_id=None, embed_fn=None):
    if action not in ("approved", "rejected"):
        raise ValueError("action must be 'approved' or 'rejected'")
    if not user_id or not reason:
        raise ValueError("user_id and reason are required")
    vec = vec_literal(embed_fn([summary])[0]) if embed_fn else None
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""INSERT INTO {TABLE} (user_id, market_id, distributor_id, sku, action, qty_cases, reason, request_id,
                                             summary, summary_tsv, embedding)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, to_tsvector('english', %s), %s::vector)
                        RETURNING decision_id, created_at""",
                    (user_id, market_id, distributor_id, sku, action, qty_cases, reason, request_id, summary, summary, vec))
        return cur.fetchone()


# Same RRF pattern as rag/search.py; entity filters apply on both arms; BM25 <@> sorts ASC
_RECALL_SQL = f"""
WITH filtered AS (
  SELECT * FROM {TABLE}
  WHERE (%(market)s::text IS NULL OR market_id = %(market)s)
    AND (%(sku)s::text IS NULL OR sku = %(sku)s)
    AND (%(dist)s::text IS NULL OR distributor_id = %(dist)s)
),
q AS (
  SELECT NULLIF(array_to_string(tsvector_to_array(to_tsvector('english', %(qtext)s)), ' | '), '')::tsquery AS tsq
),
vector_ranked AS (
  SELECT decision_id, RANK() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS rank
  FROM filtered WHERE %(qvec)s::vector IS NOT NULL AND embedding IS NOT NULL
  ORDER BY embedding <=> %(qvec)s::vector LIMIT 40
),
keyword_ranked AS (
  SELECT decision_id, RANK() OVER (ORDER BY summary_tsv <@> to_bm25query(to_tsvector('english', %(qtext)s), '{BM25_INDEX}') ASC) AS rank
  FROM filtered, q WHERE q.tsq IS NOT NULL AND summary_tsv @@ q.tsq
  ORDER BY rank LIMIT 40
)
SELECT f.decision_id, f.created_at, f.user_id AS decided_by, f.action, f.market_id, f.distributor_id, f.sku, f.qty_cases, f.reason, f.summary, f.request_id,
       COALESCE(1.0/({RRF_K} + v.rank), 0) + COALESCE(1.0/({RRF_K} + k.rank), 0) AS score
FROM filtered f
LEFT JOIN vector_ranked v USING (decision_id)
LEFT JOIN keyword_ranked k USING (decision_id)
WHERE v.decision_id IS NOT NULL OR k.decision_id IS NOT NULL
ORDER BY score DESC, f.created_at DESC
LIMIT %(top_k)s
"""


def recall_decisions(conn, user_id, query=None, market_id=None, sku=None, distributor_id=None, top_k=5, embed_fn=None):
    if not user_id:
        raise ValueError("user_id is required")
    with conn.cursor(row_factory=dict_row) as cur:
        if not query:
            # no query: most recent decisions for the entity
            cur.execute(f"""SELECT decision_id, created_at, user_id AS decided_by, action, market_id, distributor_id, sku, qty_cases, reason, summary, request_id
                            FROM {TABLE} WHERE (%s::text IS NULL OR market_id = %s)
                              AND (%s::text IS NULL OR sku = %s) AND (%s::text IS NULL OR distributor_id = %s)
                            ORDER BY created_at DESC LIMIT %s""",
                        (market_id, market_id, sku, sku, distributor_id, distributor_id, top_k))
            return cur.fetchall()
        qvec = vec_literal(embed_fn([query])[0]) if embed_fn else None
        cur.execute(_RECALL_SQL, {"user_id": user_id, "market": market_id, "sku": sku, "dist": distributor_id,
                                  "qtext": query, "qvec": qvec, "top_k": top_k})
        return cur.fetchall()
