"""Generate synthetic Acme Beverages gold tables as parquet files (local)."""
import argparse
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd

from entities import (DISTRIBUTOR_TEMPLATES, MARKET_CITY, MARKETS, POLICY,
                      PRODUCTS, PROMO_MECHANICS)

SEED = 42
WEEKS = 78
END_WEEK = date(2026, 11, 2)  # Monday


def week_starts():
    return [END_WEEK - timedelta(weeks=WEEKS - 1 - i) for i in range(WEEKS)]


def build(out_dir):
    rng = np.random.default_rng(SEED)
    os.makedirs(out_dir, exist_ok=True)

    # Dimensions
    dim_market = pd.DataFrame(MARKETS, columns=["market_id", "market_name", "region", "currency"])
    dim_product = pd.DataFrame(PRODUCTS, columns=["sku", "brand", "product_name", "pack_type",
                                                  "pack_size_ml", "units_per_case", "list_price_per_case_usd"])
    cat = {"Fizzo": "Carbonated soft drinks", "Aqua Pura": "Water", "Sunny Grove": "Juices",
           "Volt": "Energy drinks", "Harbor": "Beer"}
    dim_product["category"] = dim_product["brand"].map(cat)
    dist_rows = []
    for m, *_ in MARKETS:
        for code, channel, name in DISTRIBUTOR_TEMPLATES:
            dist_rows.append((f"{m}-{code}", name.format(city=MARKET_CITY[m]), m, channel))
    dim_distributor = pd.DataFrame(dist_rows, columns=["distributor_id", "distributor_name", "market_id", "channel"])

    # Base demand (cases/week) per market x distributor x sku
    market_scale = {"CA": 3.0, "NY": 1.8, "AZ": 1.4, "TX": 1.0, "FL": 0.9, "IL": 0.8, "GA": 0.6, "NC": 0.5}
    channel_scale = {"Modern Trade": 1.0, "Traditional Trade": 1.3, "On-Premise": 0.5}
    brand_channel = {("Harbor", "On-Premise"): 2.2, ("Volt", "Traditional Trade"): 1.2,
                     ("Aqua Pura", "Modern Trade"): 1.3}

    weeks = week_starts()
    promos, promo_id = [], 1
    promo_weeks = {}
    for m, *_ in MARKETS:
        for sku, brand, *_r in PRODUCTS:
            for _ in range(rng.integers(2, 5)):
                start_idx = int(rng.integers(0, WEEKS - 7))
                dur = int(rng.integers(2, 5))
                disc = float(rng.choice([0.10, 0.15, 0.20, 0.25]))
                mech = str(rng.choice(PROMO_MECHANICS))
                uplift = round(disc * rng.uniform(1.4, 2.6) * (1.3 if mech == "Display + feature" else 1.0), 3)
                for k in range(start_idx, min(start_idx + dur, WEEKS)):
                    promo_weeks[(m, sku, k)] = (uplift, disc)
                forecast = round(uplift * rng.normal(1.0, 0.15), 3)
                promos.append((f"PR-{promo_id:04d}", m, sku, weeks[start_idx], weeks[min(start_idx + dur, WEEKS) - 1] + timedelta(days=6),
                               disc, mech, forecast, uplift, "Completed"))
                promo_id += 1
    # Engineered in-flight promo running below forecast: display not installed at one distributor
    ACTIVE = {"promo_id": f"PR-{promo_id:04d}", "market_id": "CA", "sku": "SG-ORNG-1000T", "start_idx": WEEKS - 3,
              "discount": 0.20, "mechanic": "Display + feature", "forecast": 0.45,
              "uplift_by_dist": {"CA-D1": 0.04, "CA-D2": 0.43, "CA-D3": 0.40}}
    for k in range(ACTIVE["start_idx"], WEEKS):
        promo_weeks[(ACTIVE["market_id"], ACTIVE["sku"], k)] = (ACTIVE["forecast"], ACTIVE["discount"])
    fact_promotions = pd.DataFrame(promos, columns=["promo_id", "market_id", "sku", "start_date", "end_date", "discount_pct",
                                                    "mechanic", "uplift_forecast_pct", "uplift_actual_pct", "status"])

    dep_rows, ship_rows = [], []
    inv_rows = []
    price = dict(zip(dim_product.sku, dim_product.list_price_per_case_usd))
    for _, d in dim_distributor.iterrows():
        for sku, brand, *_r in PRODUCTS:
            base = 40 * market_scale[d.market_id] * channel_scale[d.channel] * brand_channel.get((brand, d.channel), 1.0)
            base *= rng.uniform(0.6, 1.4)
            on_hand = POLICY["target_cover_days"][d.channel] / 7.0 * base
            trend = rng.uniform(-0.002, 0.006)
            for i, ws in enumerate(weeks):
                season = 1 + 0.18 * np.sin(2 * np.pi * (ws.timetuple().tm_yday / 365.0) - 1.2)
                if brand == "Harbor" and ws.month == 12:
                    season *= 1.25
                uplift, disc = promo_weeks.get((d.market_id, sku, i), (0.0, 0.0))
                if (d.market_id, sku) == (ACTIVE["market_id"], ACTIVE["sku"]) and i >= ACTIVE["start_idx"]:
                    uplift = ACTIVE["uplift_by_dist"][d.distributor_id]
                demand = max(0.0, base * season * (1 + trend) ** i * (1 + uplift) * rng.normal(1, 0.08))
                dep = round(min(demand, on_hand + base), 1)
                target = POLICY["target_cover_days"][d.channel] / 7.0 * base * season
                ship = round(max(0.0, (target - on_hand + base * season) * rng.normal(1.0, 0.15)), 1)
                # Engineered at-risk positions in the latest weeks
                if i >= WEEKS - 3 and (d.market_id, sku) in {("TX", "VT-ORIG-473C"), ("NY", "FZ-COLA-600P"),
                                                             ("FL", "HB-LAGR-355B"), ("CA", "AP-STILL-500P")}:
                    ship = round(ship * 0.15, 1)
                on_hand = max(0.0, on_hand + ship - dep)
                dep_rows.append((ws, d.market_id, d.distributor_id, sku, dep, round(dep * price[sku] * (1 - disc), 2), uplift > 0))
                ship_rows.append((ws, d.market_id, d.distributor_id, sku, ship))
            inv_rows.append((END_WEEK + timedelta(days=6), d.market_id, d.distributor_id, sku,
                             round(on_hand, 1), round(base * rng.uniform(0.0, 0.8), 1)))

    fact_depletions = pd.DataFrame(dep_rows, columns=["week_start", "market_id", "distributor_id", "sku",
                                                      "cases_depleted", "net_revenue_usd", "on_promo"])
    fact_shipments = pd.DataFrame(ship_rows, columns=["week_start", "market_id", "distributor_id", "sku", "cases_shipped"])
    fact_inventory = pd.DataFrame(inv_rows, columns=["snapshot_date", "market_id", "distributor_id", "sku",
                                                     "on_hand_cases", "in_transit_cases"])

    # Measured uplift to date for the active promo vs the 8 pre-promo weeks
    sel = fact_depletions[(fact_depletions.market_id == ACTIVE["market_id"]) & (fact_depletions.sku == ACTIVE["sku"])]
    wk = sorted(sel.week_start.unique())
    pre = sel[sel.week_start.isin(wk[ACTIVE["start_idx"] - 8:ACTIVE["start_idx"]])].groupby("week_start").cases_depleted.sum().mean()
    dur = sel[sel.week_start.isin(wk[ACTIVE["start_idx"]:])].groupby("week_start").cases_depleted.sum().mean()
    active_row = {"promo_id": ACTIVE["promo_id"], "market_id": ACTIVE["market_id"], "sku": ACTIVE["sku"],
                  "start_date": weeks[ACTIVE["start_idx"]], "end_date": END_WEEK + timedelta(weeks=3, days=6),
                  "discount_pct": ACTIVE["discount"], "mechanic": ACTIVE["mechanic"],
                  "uplift_forecast_pct": ACTIVE["forecast"], "uplift_actual_pct": round(dur / pre - 1, 3), "status": "Active"}
    fact_promotions = pd.concat([fact_promotions, pd.DataFrame([active_row])], ignore_index=True)
    print("active promo:", active_row)

    tables = {"dim_market": dim_market, "dim_product": dim_product, "dim_distributor": dim_distributor,
              "fact_depletions": fact_depletions, "fact_shipments": fact_shipments,
              "fact_inventory": fact_inventory, "fact_promotions": fact_promotions}
    for name, df in tables.items():
        df.to_parquet(os.path.join(out_dir, f"{name}.parquet"), index=False)
        print(f"{name}: {len(df)} rows")
    print("policy:", POLICY)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "out"))
    build(ap.parse_args().out)
