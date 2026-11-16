"""RAG schema on Lakebase: extensions + doc_chunks table. Indexes are built by the loader after insert (BM25 needs rows first).
Usage: python3 setup/40_lakebase_rag.py [--fallback]   (--fallback = pgvector hnsw + gin, if Lakebase Search is off)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import config as C
from lakebase_conn import connect

SCHEMA = os.environ.get("BEV_LAKEBASE_SCHEMA", "rag")

# Extensions
EXT_SEARCH = ["CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE", "CREATE EXTENSION IF NOT EXISTS lakebase_text"]
EXT_FALLBACK = ["CREATE EXTENSION IF NOT EXISTS vector"]

# Table
DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.doc_chunks (
  chunk_id        text PRIMARY KEY,
  doc_id          text NOT NULL,
  title           text NOT NULL,
  doc_type        text NOT NULL,
  market          text NOT NULL,
  valid_from      date,
  valid_to        date,
  section         text,
  chunk_index     int  NOT NULL,
  content         text NOT NULL,
  chunk_tsvector  tsvector,
  embedding       vector({C.EMBEDDING_DIMS}),
  loaded_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS doc_chunks_filters ON {SCHEMA}.doc_chunks (market, doc_type);
"""


def main(fallback=False):
    with connect() as conn:
        for s in EXT_FALLBACK if fallback else EXT_SEARCH:
            conn.execute(s)
        conn.execute(DDL)
        conn.commit()
        print(conn.execute("select extname, extversion from pg_extension order by 1").fetchall())
        print(conn.execute(f"select count(*) from {SCHEMA}.doc_chunks").fetchone())


if __name__ == "__main__":
    main(fallback="--fallback" in sys.argv)
