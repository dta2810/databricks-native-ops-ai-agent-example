"""Hybrid retrieval on Lakebase: lakebase_ann (vector) + lakebase_bm25 (keyword) fused with RRF. Shared by loader, eval and agent app."""
import requests

SCHEMA = "rag"
BM25_INDEX = f"{SCHEMA}.doc_chunks_bm25"
RRF_K = 60
CANDIDATES = 40


def embed(texts, host, headers, service, step="embed"):
    # Gateway embedding service, batched, with request tags
    try:
        from agent.gateway_tags import header
        headers = dict(headers) | header(step)
    except ImportError:  # setup scripts run without the repo root on sys.path
        headers = dict(headers)
    out = []
    for i in range(0, len(texts), 16):
        r = requests.post(f"{host}/ai-gateway/mlflow/v1/embeddings", headers=headers, timeout=120,
                          json={"model": service, "input": texts[i:i + 16]})
        r.raise_for_status()
        out += [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
    return out


def vec_literal(v):
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


# Keyword side uses an OR tsquery so long questions still match; BM25 ranks the hits
HYBRID_SQL = f"""
WITH filtered AS (
  SELECT * FROM {SCHEMA}.doc_chunks
  WHERE (%(market)s::text IS NULL OR market IN (%(market)s, 'ALL'))
    AND (%(doc_types)s::text[] IS NULL OR doc_type = ANY(%(doc_types)s::text[]))
    AND (valid_to IS NULL OR valid_to >= COALESCE(%(as_of)s::date, current_date))
),
q AS (
  SELECT NULLIF(array_to_string(tsvector_to_array(to_tsvector('english', %(qtext)s)), ' | '), '')::tsquery AS tsq
),
vector_ranked AS (
  SELECT chunk_id, RANK() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS rank
  FROM filtered ORDER BY embedding <=> %(qvec)s::vector LIMIT {CANDIDATES}
),
keyword_ranked AS (
  SELECT chunk_id, RANK() OVER (ORDER BY chunk_tsvector <@> to_bm25query(to_tsvector('english', %(qtext)s), '{BM25_INDEX}') {{bm25_order}}) AS rank
  FROM filtered, q
  WHERE q.tsq IS NOT NULL AND chunk_tsvector @@ q.tsq
  ORDER BY rank LIMIT {CANDIDATES}
)
SELECT f.chunk_id, f.doc_id, f.title, f.doc_type, f.market, f.section, f.content,
       v.rank AS vector_rank, k.rank AS keyword_rank,
       COALESCE(1.0/({RRF_K} + v.rank), 0) + COALESCE(1.0/({RRF_K} + k.rank), 0) AS rrf_score
FROM filtered f
LEFT JOIN vector_ranked v USING (chunk_id)
LEFT JOIN keyword_ranked k USING (chunk_id)
WHERE v.chunk_id IS NOT NULL OR k.chunk_id IS NOT NULL
ORDER BY rrf_score DESC
LIMIT %(top_k)s
"""

# BM25 sort direction for the <@> operator, confirmed empirically by eval_retrieval.py --check-order
BM25_ORDER = "ASC"


# Promotion results are explained by distributor field reports, so a promo brief search also reads them
ALSO_SEARCH = {"promo_brief": ["field_report"]}


def doc_type_filter(value, allowed):
    # "promo_brief,field_report" or a list -> known types (+ ALSO_SEARCH); None = no filter
    raw = value.split(",") if isinstance(value, str) else (value or [])
    types = [t.strip() for t in raw if t and t.strip() in allowed]
    for t in list(types):
        types += [r for r in ALSO_SEARCH.get(t, []) if r not in types]
    return types or None


def search(conn, query, qvec, market=None, doc_types=None, as_of=None, top_k=5, bm25_order=BM25_ORDER):
    sql = HYBRID_SQL.replace("{bm25_order}", bm25_order)
    cur = conn.execute(sql, {"qtext": query, "qvec": vec_literal(qvec), "market": market,
                             "doc_types": doc_types, "as_of": as_of, "top_k": top_k})
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
