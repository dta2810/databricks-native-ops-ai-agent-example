-- Layer 1: Unity Catalog documentation, keys and helper views (Genie reads all of this)
COMMENT ON TABLE ${catalog}.${schema}.dim_market IS 'One row per market (country) where Acme Beverages sells. Fictitious company. Key: market_id.';
ALTER TABLE ${catalog}.${schema}.dim_market ALTER COLUMN market_id COMMENT 'Market code (US state code), e.g. CA, TX, NY.';
ALTER TABLE ${catalog}.${schema}.dim_market ALTER COLUMN market_name COMMENT 'Market (country) name.';
ALTER TABLE ${catalog}.${schema}.dim_market ALTER COLUMN region COMMENT 'Commercial region: West, South Central, Midwest, Southeast, Northeast, Southwest.';
ALTER TABLE ${catalog}.${schema}.dim_market ALTER COLUMN currency COMMENT 'Local currency code. All revenue in this dataset is reported in USD.';
COMMENT ON TABLE ${catalog}.${schema}.dim_product IS 'One row per SKU in the Acme Beverages portfolio (soft drinks, water, juices, energy drinks, beer). Key: sku.';
ALTER TABLE ${catalog}.${schema}.dim_product ALTER COLUMN sku COMMENT 'Stock keeping unit code, e.g. FZ-COLA-600P. Pattern: brand prefix, flavour, pack size and pack type.';
ALTER TABLE ${catalog}.${schema}.dim_product ALTER COLUMN brand COMMENT 'Brand: Fizzo (soft drinks), Aqua Pura (water), Sunny Grove (juices), Volt (energy drinks), Harbor (beer).';
ALTER TABLE ${catalog}.${schema}.dim_product ALTER COLUMN product_name COMMENT 'Full product name including pack, e.g. Fizzo Cola 600ml PET.';
ALTER TABLE ${catalog}.${schema}.dim_product ALTER COLUMN pack_type COMMENT 'Pack type: Can, PET, Glass, Tetra.';
ALTER TABLE ${catalog}.${schema}.dim_product ALTER COLUMN pack_size_ml COMMENT 'Unit size in millilitres.';
ALTER TABLE ${catalog}.${schema}.dim_product ALTER COLUMN units_per_case COMMENT 'Units per case. Volumes in this dataset are in cases.';
ALTER TABLE ${catalog}.${schema}.dim_product ALTER COLUMN list_price_per_case_usd COMMENT 'List price per case in USD before promotional discount.';
ALTER TABLE ${catalog}.${schema}.dim_product ALTER COLUMN category COMMENT 'Category: Carbonated soft drinks, Water, Juices, Energy drinks, Beer.';
COMMENT ON TABLE ${catalog}.${schema}.dim_distributor IS 'One row per distributor. Each market has three: Modern Trade (D1), Traditional Trade (D2) and On-Premise (D3). Key: distributor_id.';
ALTER TABLE ${catalog}.${schema}.dim_distributor ALTER COLUMN distributor_id COMMENT 'Distributor code: market code plus D1/D2/D3, e.g. CA-D1.';
ALTER TABLE ${catalog}.${schema}.dim_distributor ALTER COLUMN distributor_name COMMENT 'Distributor name, e.g. Golden Coast Retail Partners.';
ALTER TABLE ${catalog}.${schema}.dim_distributor ALTER COLUMN market_id COMMENT 'Market the distributor serves. Joins dim_market.market_id.';
ALTER TABLE ${catalog}.${schema}.dim_distributor ALTER COLUMN channel COMMENT 'Route to market: Modern Trade (supermarkets), Traditional Trade (small stores), On-Premise (bars, restaurants, hotels).';
ALTER TABLE ${catalog}.${schema}.fact_depletions ALTER COLUMN week_start COMMENT 'Monday of the week.';
ALTER TABLE ${catalog}.${schema}.fact_depletions ALTER COLUMN market_id COMMENT 'Market code. Joins dim_market.';
ALTER TABLE ${catalog}.${schema}.fact_depletions ALTER COLUMN distributor_id COMMENT 'Distributor code. Joins dim_distributor.';
ALTER TABLE ${catalog}.${schema}.fact_depletions ALTER COLUMN sku COMMENT 'SKU code. Joins dim_product.';
ALTER TABLE ${catalog}.${schema}.fact_shipments ALTER COLUMN week_start COMMENT 'Monday of the week.';
ALTER TABLE ${catalog}.${schema}.fact_shipments ALTER COLUMN market_id COMMENT 'Market code. Joins dim_market.';
ALTER TABLE ${catalog}.${schema}.fact_shipments ALTER COLUMN distributor_id COMMENT 'Distributor code. Joins dim_distributor.';
ALTER TABLE ${catalog}.${schema}.fact_shipments ALTER COLUMN sku COMMENT 'SKU code. Joins dim_product.';
ALTER TABLE ${catalog}.${schema}.fact_inventory ALTER COLUMN snapshot_date COMMENT 'Date of the inventory snapshot (latest week end).';
ALTER TABLE ${catalog}.${schema}.fact_inventory ALTER COLUMN market_id COMMENT 'Market code. Joins dim_market.';
ALTER TABLE ${catalog}.${schema}.fact_inventory ALTER COLUMN distributor_id COMMENT 'Distributor code. Joins dim_distributor.';
ALTER TABLE ${catalog}.${schema}.fact_inventory ALTER COLUMN sku COMMENT 'SKU code. Joins dim_product.';
ALTER TABLE ${catalog}.${schema}.fact_promotions ALTER COLUMN promo_id COMMENT 'Promotion code, e.g. PR-0422.';
ALTER TABLE ${catalog}.${schema}.fact_promotions ALTER COLUMN market_id COMMENT 'Market code. Joins dim_market.';
ALTER TABLE ${catalog}.${schema}.fact_promotions ALTER COLUMN sku COMMENT 'Promoted SKU. Joins dim_product.';
ALTER TABLE ${catalog}.${schema}.fact_promotions ALTER COLUMN start_date COMMENT 'First day of the promotion.';
ALTER TABLE ${catalog}.${schema}.fact_promotions ALTER COLUMN end_date COMMENT 'Last day of the promotion (can be in the future for Active promos).';
ALTER TABLE ${catalog}.${schema}.fact_promotions ALTER COLUMN discount_pct COMMENT 'Price discount as a fraction (0.20 = 20% off).';
ALTER TABLE ${catalog}.${schema}.fact_promotions ALTER COLUMN mechanic COMMENT 'Promotion mechanic: Price discount, Multi-buy 2x1, Bundle with snack, Display + feature.';
ALTER TABLE ${catalog}.${schema}.replenishment_requests ALTER COLUMN request_id COMMENT 'Request identifier.';
ALTER TABLE ${catalog}.${schema}.replenishment_requests ALTER COLUMN status COMMENT 'Pending, Approved or Rejected.';
ALTER TABLE ${catalog}.${schema}.dim_market ALTER COLUMN market_id SET NOT NULL;
ALTER TABLE ${catalog}.${schema}.dim_product ALTER COLUMN sku SET NOT NULL;
ALTER TABLE ${catalog}.${schema}.dim_distributor ALTER COLUMN distributor_id SET NOT NULL;
ALTER TABLE ${catalog}.${schema}.fact_promotions ALTER COLUMN promo_id SET NOT NULL;
ALTER TABLE ${catalog}.${schema}.dim_market DROP CONSTRAINT IF EXISTS pk_dim_market;
ALTER TABLE ${catalog}.${schema}.dim_market ADD CONSTRAINT pk_dim_market PRIMARY KEY (market_id);
ALTER TABLE ${catalog}.${schema}.dim_product DROP CONSTRAINT IF EXISTS pk_dim_product;
ALTER TABLE ${catalog}.${schema}.dim_product ADD CONSTRAINT pk_dim_product PRIMARY KEY (sku);
ALTER TABLE ${catalog}.${schema}.dim_distributor DROP CONSTRAINT IF EXISTS pk_dim_distributor;
ALTER TABLE ${catalog}.${schema}.dim_distributor ADD CONSTRAINT pk_dim_distributor PRIMARY KEY (distributor_id);
ALTER TABLE ${catalog}.${schema}.fact_promotions DROP CONSTRAINT IF EXISTS pk_fact_promotions;
ALTER TABLE ${catalog}.${schema}.fact_promotions ADD CONSTRAINT pk_fact_promotions PRIMARY KEY (promo_id);
ALTER TABLE ${catalog}.${schema}.dim_distributor DROP CONSTRAINT IF EXISTS fk_distributor_market;
ALTER TABLE ${catalog}.${schema}.dim_distributor ADD CONSTRAINT fk_distributor_market FOREIGN KEY (market_id) REFERENCES ${catalog}.${schema}.dim_market;
ALTER TABLE ${catalog}.${schema}.fact_inventory DROP CONSTRAINT IF EXISTS fk_inventory_distributor;
ALTER TABLE ${catalog}.${schema}.fact_inventory ADD CONSTRAINT fk_inventory_distributor FOREIGN KEY (distributor_id) REFERENCES ${catalog}.${schema}.dim_distributor;
ALTER TABLE ${catalog}.${schema}.fact_inventory DROP CONSTRAINT IF EXISTS fk_inventory_product;
ALTER TABLE ${catalog}.${schema}.fact_inventory ADD CONSTRAINT fk_inventory_product FOREIGN KEY (sku) REFERENCES ${catalog}.${schema}.dim_product;
ALTER TABLE ${catalog}.${schema}.fact_promotions DROP CONSTRAINT IF EXISTS fk_promotions_product;
ALTER TABLE ${catalog}.${schema}.fact_promotions ADD CONSTRAINT fk_promotions_product FOREIGN KEY (sku) REFERENCES ${catalog}.${schema}.dim_product;
ALTER TABLE ${catalog}.${schema}.fact_promotions DROP CONSTRAINT IF EXISTS fk_promotions_market;
ALTER TABLE ${catalog}.${schema}.fact_promotions ADD CONSTRAINT fk_promotions_market FOREIGN KEY (market_id) REFERENCES ${catalog}.${schema}.dim_market;
CREATE OR REPLACE VIEW ${catalog}.${schema}.v_stock_cover
COMMENT 'Current stock cover per distributor and SKU. cover_days = (on hand + in transit) / average daily depletions of the last 8 weeks. status: AT RISK below 7 days, BELOW TARGET under the channel target (Modern Trade 21, Traditional Trade 14, On-Premise 10), else OK. Use for out-of-stock and stock cover questions.'
AS
WITH dep AS (
  SELECT distributor_id, sku, avg(cases_depleted) AS avg_weekly_depletion_8w, avg(net_revenue_usd) AS avg_weekly_revenue_8w
  FROM ${catalog}.${schema}.fact_depletions
  WHERE week_start > (SELECT max(week_start) FROM ${catalog}.${schema}.fact_depletions) - INTERVAL 56 DAYS
  GROUP BY distributor_id, sku)
SELECT i.snapshot_date, i.market_id, d.distributor_id, d.distributor_name, d.channel, i.sku, p.product_name, p.brand,
       i.on_hand_cases, i.in_transit_cases, round(dep.avg_weekly_depletion_8w, 1) AS avg_weekly_depletion_8w,
       round((i.on_hand_cases + i.in_transit_cases) / greatest(dep.avg_weekly_depletion_8w / 7.0, 0.1), 1) AS cover_days,
       CASE d.channel WHEN 'Modern Trade' THEN 21 WHEN 'Traditional Trade' THEN 14 ELSE 10 END AS target_cover_days,
       round(dep.avg_weekly_revenue_8w, 0) AS weekly_revenue_at_risk_usd,
       CASE WHEN (i.on_hand_cases + i.in_transit_cases) / greatest(dep.avg_weekly_depletion_8w / 7.0, 0.1) < 7 THEN 'AT RISK'
            WHEN (i.on_hand_cases + i.in_transit_cases) / greatest(dep.avg_weekly_depletion_8w / 7.0, 0.1)
                 < CASE d.channel WHEN 'Modern Trade' THEN 21 WHEN 'Traditional Trade' THEN 14 ELSE 10 END THEN 'BELOW TARGET'
            ELSE 'OK' END AS status
FROM ${catalog}.${schema}.fact_inventory i
JOIN ${catalog}.${schema}.dim_distributor d ON i.distributor_id = d.distributor_id
JOIN ${catalog}.${schema}.dim_product p ON i.sku = p.sku
JOIN dep ON dep.distributor_id = i.distributor_id AND dep.sku = i.sku;
CREATE OR REPLACE VIEW ${catalog}.${schema}.v_promo_distributor_uplift
COMMENT 'Measured uplift of every promotion per distributor: average weekly depletions during the promo vs the 8 weeks before. Use to see where a promotion under- or over-delivers against its forecast.'
AS
WITH dep AS (
  SELECT pr.promo_id, f.distributor_id,
         avg(CASE WHEN f.week_start < pr.start_date AND f.week_start >= pr.start_date - INTERVAL 56 DAYS THEN f.cases_depleted END) AS pre_promo_avg_cases,
         avg(CASE WHEN f.week_start >= pr.start_date AND f.week_start <= pr.end_date THEN f.cases_depleted END) AS in_promo_avg_cases
  FROM ${catalog}.${schema}.fact_promotions pr
  JOIN ${catalog}.${schema}.fact_depletions f ON f.market_id = pr.market_id AND f.sku = pr.sku
  GROUP BY pr.promo_id, f.distributor_id)
SELECT pr.promo_id, pr.status, pr.market_id, pr.sku, pr.mechanic, pr.discount_pct, pr.uplift_forecast_pct,
       dep.distributor_id, d.distributor_name, d.channel,
       round(dep.pre_promo_avg_cases, 1) AS pre_promo_avg_cases, round(dep.in_promo_avg_cases, 1) AS in_promo_avg_cases,
       round(dep.in_promo_avg_cases / NULLIF(dep.pre_promo_avg_cases, 0) - 1, 3) AS uplift_actual_pct,
       round(dep.in_promo_avg_cases / NULLIF(dep.pre_promo_avg_cases, 0) - 1 - pr.uplift_forecast_pct, 3) AS gap_vs_forecast_pct
FROM dep
JOIN ${catalog}.${schema}.fact_promotions pr ON pr.promo_id = dep.promo_id
JOIN ${catalog}.${schema}.dim_distributor d ON d.distributor_id = dep.distributor_id;
CREATE OR REPLACE VIEW ${catalog}.${schema}.shipments_metrics
WITH METRICS
LANGUAGE YAML
COMMENT 'Governed sell-in metrics for Acme Beverages: shipments in cases from Acme Beverages to distributors, by week, market, distributor, channel, brand and SKU.'
AS $$
version: 1.1
source: ${catalog}.${schema}.fact_shipments
joins:
  - name: product
    source: ${catalog}.${schema}.dim_product
    on: source.sku = product.sku
  - name: distributor
    source: ${catalog}.${schema}.dim_distributor
    on: source.distributor_id = distributor.distributor_id
  - name: market
    source: ${catalog}.${schema}.dim_market
    on: source.market_id = market.market_id
dimensions:
  - name: week_start
    expr: source.week_start
    display_name: Week
  - name: market_name
    expr: market.market_name
    display_name: Market
    synonyms: [country]
  - name: region
    expr: market.region
  - name: distributor_name
    expr: distributor.distributor_name
    display_name: Distributor
  - name: channel
    expr: distributor.channel
    synonyms: [route to market, trade channel]
  - name: brand
    expr: product.brand
  - name: category
    expr: product.category
  - name: sku
    expr: source.sku
  - name: product_name
    expr: product.product_name
    display_name: Product
measures:
  - name: cases_shipped
    expr: SUM(source.cases_shipped)
    display_name: Cases shipped
    synonyms: [sell-in, shipments, sell in volume]
$$;
CREATE OR REPLACE VIEW ${catalog}.${schema}.depletions_metrics
WITH METRICS
LANGUAGE YAML
COMMENT 'Governed sell-out metrics for Acme Beverages: depletions in cases and net revenue USD by week, market, distributor, channel, brand and SKU.'
AS $$
version: 1.1
source: ${catalog}.${schema}.fact_depletions
joins:
  - name: product
    source: ${catalog}.${schema}.dim_product
    on: source.sku = product.sku
  - name: distributor
    source: ${catalog}.${schema}.dim_distributor
    on: source.distributor_id = distributor.distributor_id
  - name: market
    source: ${catalog}.${schema}.dim_market
    on: source.market_id = market.market_id
dimensions:
  - name: week_start
    expr: source.week_start
    display_name: Week
  - name: market_name
    expr: market.market_name
    display_name: Market
    synonyms: [country]
  - name: region
    expr: market.region
  - name: distributor_name
    expr: distributor.distributor_name
    display_name: Distributor
  - name: channel
    expr: distributor.channel
    synonyms: [route to market, trade channel]
  - name: brand
    expr: product.brand
  - name: category
    expr: product.category
  - name: sku
    expr: source.sku
  - name: product_name
    expr: product.product_name
    display_name: Product
  - name: on_promo
    expr: source.on_promo
    display_name: On promotion
measures:
  - name: cases_depleted
    expr: SUM(source.cases_depleted)
    display_name: Cases depleted
    synonyms: [depletions, sell-out, sell out volume, volume]
  - name: net_revenue_usd
    expr: SUM(source.net_revenue_usd)
    display_name: Net revenue (USD)
    synonyms: [revenue, sales, net sales]
  - name: promo_share_of_cases
    expr: SUM(CASE WHEN source.on_promo THEN source.cases_depleted ELSE 0 END) / NULLIF(SUM(source.cases_depleted), 0)
    display_name: Promo share of cases
  - name: avg_net_price_per_case_usd
    expr: SUM(source.net_revenue_usd) / NULLIF(SUM(source.cases_depleted), 0)
    display_name: Average net price per case (USD)
$$;
SELECT status, count(*) FROM ${catalog}.${schema}.v_stock_cover GROUP BY status;
SELECT distributor_id, uplift_actual_pct, gap_vs_forecast_pct FROM ${catalog}.${schema}.v_promo_distributor_uplift WHERE promo_id = 'PR-0422';
SELECT brand, MEASURE(cases_shipped) FROM ${catalog}.${schema}.shipments_metrics GROUP BY brand
