"""Chunk docs_corpus/*.md by section, embed via Gateway, reload rag.doc_chunks, rebuild ANN + BM25 indexes (idempotent).
Canonical docs are rendered through demo_clock.shift_text with the live data delta (dates + doc ids follow the data).
Usage: python3 setup/41_load_corpus.py [--fallback]
"""
import glob
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, ROOT)
import config as C
from lakebase_conn import connect
from rag.search import SCHEMA, BM25_INDEX, embed, vec_literal
from demo_clock import current_delta_days, shift_text
from sqlrun import client, run

MAX_WORDS = 220


def parse(path, delta=0):
    text = shift_text(open(path).read(), delta)
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    meta = {k.strip(): v.strip().strip('"') for k, v in (l.split(":", 1) for l in m.group(1).splitlines() if ":" in l)}
    return meta, m.group(2)


def chunks(meta, body):
    # one chunk per ## section, split long sections by paragraph
    sections = re.split(r"^##\s+", body, flags=re.M)
    out = []
    for i, sec in enumerate(sections):
        lines = sec.strip().splitlines()
        if not lines:
            continue
        heading = "Overview" if i == 0 else lines[0].strip()
        paras = [p for p in (lines if i == 0 else lines[1:]) if p.strip() and not p.startswith("# ")]
        buf = []
        for p in paras + [None]:
            if p is None or (buf and len(" ".join(buf + [p]).split()) > MAX_WORDS):
                if buf:
                    out.append((heading, "\n".join(buf)))
                buf = []
            if p is not None:
                buf.append(p)
    return [{"chunk_id": f"{meta['doc_id']}#{n:02d}", "section": h, "chunk_index": n,
             "content": f"{meta['title']} | {h}\n{c}"} for n, (h, c) in enumerate(out)]


def main(fallback=False):
    rows = []
    w = client()
    delta = current_delta_days(run, w)
    print(f"demo clock delta: {delta} days")
    for path in sorted(glob.glob(os.path.join(ROOT, "docs_corpus", "*.md"))):
        if os.path.basename(path) == "README.md":
            continue
        meta, body = parse(path, delta)
        for ch in chunks(meta, body):
            rows.append({**ch, **{k: meta.get(k) for k in ("doc_id", "title", "doc_type", "market", "valid_from", "valid_to")}})
    vecs = embed([r["content"] for r in rows], w.config.host, w.config.authenticate(), C.EMBEDDING_SERVICE)
    with connect() as conn:
        conn.execute(f"DROP INDEX IF EXISTS {BM25_INDEX}")
        conn.execute(f"DROP INDEX IF EXISTS {SCHEMA}.doc_chunks_ann")
        conn.execute(f"TRUNCATE {SCHEMA}.doc_chunks")
        with conn.cursor() as cur:
            cur.executemany(
                f"""INSERT INTO {SCHEMA}.doc_chunks (chunk_id, doc_id, title, doc_type, market, valid_from, valid_to, section, chunk_index, content, chunk_tsvector, embedding)
                    VALUES (%(chunk_id)s, %(doc_id)s, %(title)s, %(doc_type)s, %(market)s, %(valid_from)s, %(valid_to)s, %(section)s, %(chunk_index)s, %(content)s,
                            to_tsvector('english', %(content)s), %(vec)s::vector)""",
                [{**r, "vec": vec_literal(v)} for r, v in zip(rows, vecs)])
        # indexes after insert (BM25 corpus stats are computed at build time)
        if fallback:
            conn.execute(f"CREATE INDEX doc_chunks_ann ON {SCHEMA}.doc_chunks USING hnsw (embedding vector_cosine_ops)")
            conn.execute(f"CREATE INDEX doc_chunks_bm25 ON {SCHEMA}.doc_chunks USING gin (chunk_tsvector)")
        else:
            conn.execute(f"CREATE INDEX doc_chunks_ann ON {SCHEMA}.doc_chunks USING lakebase_ann (embedding vector_cosine_ops)")
            conn.execute(f"CREATE INDEX doc_chunks_bm25 ON {SCHEMA}.doc_chunks USING lakebase_bm25 (chunk_tsvector)")
        conn.commit()
        print(conn.execute(f"select count(*), count(distinct doc_id) from {SCHEMA}.doc_chunks").fetchone())


if __name__ == "__main__":
    main(fallback="--fallback" in sys.argv)
