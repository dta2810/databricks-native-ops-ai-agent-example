"""Retrieval eval over docs_corpus/README.md questions: hit@1 / hit@3 for vector-only, keyword-only and hybrid RRF.
Usage: python3 rag/eval_retrieval.py [--check-order] [--explain]
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "setup"))
import config as C
from lakebase_conn import connect
from rag.search import BM25_ORDER, embed, search
from sqlrun import client


def questions():
    rows = []
    for line in open(os.path.join(ROOT, "docs_corpus", "README.md")):
        m = re.match(r"^\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*([A-Z0-9-]+)", line)
        if m:
            rows.append((int(m.group(1)), m.group(2), m.group(3)))
    return rows


def docs_ranked(results, key=None):
    if key:
        results = sorted([r for r in results if r[key] is not None], key=lambda r: r[key])
    seen = []
    for r in results:
        if r["doc_id"] not in seen:
            seen.append(r["doc_id"])
    return seen


def main(orders, explain=False):
    qs = questions()
    w = client()
    vecs = embed([q for _, q, _ in qs], w.config.host, w.config.authenticate(), C.EMBEDDING_SERVICE)
    with connect() as conn:
        if explain:
            plan = conn.execute("EXPLAIN SELECT chunk_id FROM rag.doc_chunks ORDER BY embedding <=> (SELECT embedding FROM rag.doc_chunks LIMIT 1) LIMIT 5").fetchall()
            print("\n".join(p[0] for p in plan))
        for order in orders:
            score = {"vector": [0, 0], "keyword": [0, 0], "hybrid": [0, 0]}
            print(f"\n== BM25 order {order}")
            for (n, q, exp), v in zip(qs, vecs):
                res = search(conn, q, v, top_k=40, bm25_order=order)
                ranks = {"vector": docs_ranked(res, "vector_rank"), "keyword": docs_ranked(res, "keyword_rank"), "hybrid": docs_ranked(res)}
                for k, d in ranks.items():
                    score[k][0] += d[:1] == [exp]
                    score[k][1] += exp in d[:3]
                print(f"Q{n:>2} exp={exp:<18} hybrid={ranks['hybrid'][:3]} kw1={ranks['keyword'][:1]} vec1={ranks['vector'][:1]}")
            for k, (h1, h3) in score.items():
                print(f"{k:<8} hit@1 {h1}/{len(qs)}  hit@3 {h3}/{len(qs)}")


if __name__ == "__main__":
    main(["ASC", "DESC"] if "--check-order" in sys.argv else [BM25_ORDER], explain="--explain" in sys.argv)
