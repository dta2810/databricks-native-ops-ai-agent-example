"""Lakebase DDL for decision memory (agent_mem) and HITL replenishment requests (agent_ops), plus reference lookups from UC dims.
Runs as the table OWNER (deployer). Full columns in CREATE; the app never ALTERs at runtime. Idempotent.
Usage: python3 setup/50_lakebase_memory.py            (DDL + refresh reference tables)
       python3 setup/50_lakebase_memory.py --indexes  (build ANN + BM25 on decision_log, after rows exist)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import config as C
from lakebase_conn import connect
from sqlrun import run

MEM, OPS = "agent_mem", "agent_ops"

DDL = f"""
CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE;
CREATE EXTENSION IF NOT EXISTS lakebase_text;
CREATE SCHEMA IF NOT EXISTS {MEM};
CREATE SCHEMA IF NOT EXISTS {OPS};

CREATE TABLE IF NOT EXISTS {OPS}.ref_distributor (
  distributor_id   text PRIMARY KEY,
  distributor_name text NOT NULL,
  market_id        text NOT NULL,
  channel          text NOT NULL,
  refreshed_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {OPS}.ref_product (
  sku            text PRIMARY KEY,
  product_name   text NOT NULL,
  units_per_case int  NOT NULL,
  refreshed_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {OPS}.replenishment_requests (
  request_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at      timestamptz NOT NULL DEFAULT now(),
  requested_by    text NOT NULL,
  market_id       text NOT NULL,
  distributor_id  text NOT NULL REFERENCES {OPS}.ref_distributor (distributor_id),
  sku             text NOT NULL REFERENCES {OPS}.ref_product (sku),
  qty_cases       numeric(12,1) NOT NULL CHECK (qty_cases > 0),
  reason          text NOT NULL,
  status          text NOT NULL DEFAULT 'proposed',
  decided_by      text,
  decided_at      timestamptz,
  decision_reason text,
  thread_id       text,
  trace_id        text,
  CHECK ((status = 'proposed') = (decided_at IS NULL))
);
-- requesters may withdraw or amend their own proposed requests (migration-safe on existing tables)
ALTER TABLE {OPS}.replenishment_requests ADD COLUMN IF NOT EXISTS amended_at timestamptz;
ALTER TABLE {OPS}.replenishment_requests DROP CONSTRAINT IF EXISTS replenishment_requests_status_check;
ALTER TABLE {OPS}.replenishment_requests ADD CONSTRAINT replenishment_requests_status_check
  CHECK (status IN ('proposed', 'approved', 'rejected', 'withdrawn'));
CREATE INDEX IF NOT EXISTS rr_status ON {OPS}.replenishment_requests (status, created_at DESC);
CREATE INDEX IF NOT EXISTS rr_entity ON {OPS}.replenishment_requests (distributor_id, sku);

CREATE TABLE IF NOT EXISTS {MEM}.decision_log (
  decision_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at     timestamptz NOT NULL DEFAULT now(),
  user_id        text NOT NULL,
  market_id      text,
  distributor_id text,
  sku            text,
  action         text NOT NULL CHECK (action IN ('approved', 'rejected')),
  qty_cases      numeric(12,1),
  reason         text NOT NULL,
  request_id     uuid,
  summary        text NOT NULL,
  summary_tsv    tsvector NOT NULL,
  embedding      vector({C.EMBEDDING_DIMS})
);
CREATE INDEX IF NOT EXISTS dl_user_entity ON {MEM}.decision_log (user_id, market_id, sku, created_at DESC);
"""

# Built after rows exist: BM25 corpus stats are computed at build time
INDEXES = [
    f"DROP INDEX IF EXISTS {MEM}.decision_log_ann",
    f"DROP INDEX IF EXISTS {MEM}.decision_log_bm25",
    f"CREATE INDEX decision_log_ann ON {MEM}.decision_log USING lakebase_ann (embedding vector_cosine_ops)",
    f"CREATE INDEX decision_log_bm25 ON {MEM}.decision_log USING lakebase_bm25 (summary_tsv)",
]


def refresh_reference(conn):
    # UC dims are the source of truth; small lookups copied so approvals validate in one Lakebase transaction
    t = f"{C.CATALOG}.{C.SCHEMA}"
    dists = run(f"SELECT distributor_id, distributor_name, market_id, channel FROM {t}.dim_distributor")
    prods = run(f"SELECT sku, product_name, CAST(units_per_case AS INT) FROM {t}.dim_product")
    with conn.cursor() as cur:
        cur.executemany(f"""INSERT INTO {OPS}.ref_distributor (distributor_id, distributor_name, market_id, channel) VALUES (%s,%s,%s,%s)
                            ON CONFLICT (distributor_id) DO UPDATE SET distributor_name=EXCLUDED.distributor_name,
                            market_id=EXCLUDED.market_id, channel=EXCLUDED.channel, refreshed_at=now()""", dists)
        cur.executemany(f"""INSERT INTO {OPS}.ref_product (sku, product_name, units_per_case) VALUES (%s,%s,%s)
                            ON CONFLICT (sku) DO UPDATE SET product_name=EXCLUDED.product_name,
                            units_per_case=EXCLUDED.units_per_case, refreshed_at=now()""", prods)
    return len(dists), len(prods)


def main(indexes=False):
    with connect() as conn:
        if indexes:
            for s in INDEXES:
                conn.execute(s)
            conn.commit()
            print("decision_log indexes rebuilt")
            return
        conn.execute(DDL)
        print("reference rows (distributors, products):", refresh_reference(conn))
        conn.commit()
        print(conn.execute("""SELECT table_schema, table_name FROM information_schema.tables
                              WHERE table_schema IN (%s, %s) ORDER BY 1, 2""", (MEM, OPS)).fetchall())


if __name__ == "__main__":
    main(indexes="--indexes" in sys.argv)
