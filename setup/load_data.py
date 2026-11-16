"""Upload generated parquet files to the UC Volume and (re)create the gold Delta tables."""
import glob
import os

import config
from sqlrun import client, run

COMMENTS = {
    "dim_market": "Markets where Acme Beverages operates (fictitious company).",
    "dim_product": "Product catalogue: SKU, brand, category, pack and list price per case in USD.",
    "dim_distributor": "Distributors per market and their channel (Modern Trade, Traditional Trade, On-Premise).",
    "fact_depletions": "Weekly depletions (sell-out from distributor to retail) in cases and net revenue USD, per market, distributor and SKU.",
    "fact_shipments": "Weekly shipments (sell-in from Acme Beverages to distributor) in cases.",
    "fact_inventory": "Latest distributor inventory snapshot: on-hand and in-transit cases.",
    "fact_promotions": "Trade promotions per market and SKU: discount, mechanic, forecast vs measured uplift and status (Completed or Active).",
}


def main():
    w = client()
    data_dir = os.path.join(os.path.dirname(__file__), "..", "data", "out")
    vol = f"/Volumes/{config.CATALOG}/{config.SCHEMA}/{config.VOLUME}/gold"
    for path in sorted(glob.glob(os.path.join(data_dir, "*.parquet"))):
        name = os.path.basename(path).replace(".parquet", "")
        with open(path, "rb") as fh:
            w.files.upload(f"{vol}/{name}.parquet", fh, overwrite=True)
        fq = f"{config.CATALOG}.{config.SCHEMA}.{name}"
        run(f"CREATE OR REPLACE TABLE {fq} COMMENT '{COMMENTS[name]}' AS "
            f"SELECT * FROM read_files('{vol}/{name}.parquet', format => 'parquet')", w)
        print(f"{fq}: {run(f'SELECT count(*) FROM {fq}', w)[0][0]} rows")
    run(f"""CREATE TABLE IF NOT EXISTS {config.CATALOG}.{config.SCHEMA}.replenishment_requests (
        request_id STRING, created_at TIMESTAMP, requested_by STRING, market_id STRING, distributor_id STRING,
        sku STRING, qty_cases DOUBLE, reason STRING, status STRING, approved_by STRING)
        COMMENT 'Replenishment requests created by the agent after human approval.'""", w)
    print("replenishment_requests ready")


if __name__ == "__main__":
    main()
