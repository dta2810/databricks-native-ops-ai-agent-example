-- Layer 1 for "Acme Beverages - Supply & Replenishment". Additive only.
-- Parameters: ${catalog}, ${schema}. Substitute before running.

-- 1. Weekly sell-in vs sell-out per distributor and SKU (stock build = shipped - depleted).
CREATE OR REPLACE VIEW ${catalog}.${schema}.v_sell_in_out_weekly (
  week_start COMMENT 'Monday of the week.',
  market_id COMMENT 'Market code. Joins dim_market.market_id.',
  market_name COMMENT 'Market (country) name.',
  distributor_id COMMENT 'Distributor code, e.g. CA-D1. Joins dim_distributor.distributor_id.',
  distributor_name COMMENT 'Distributor name.',
  channel COMMENT 'Route to market: Modern Trade, Traditional Trade or On-Premise.',
  sku COMMENT 'SKU code. Joins dim_product.sku.',
  product_name COMMENT 'Full product name including pack.',
  brand COMMENT 'Brand: Fizzo, Aqua Pura, Sunny Grove, Volt or Harbor.',
  cases_shipped COMMENT 'Sell-in: cases shipped from Acme Beverages to the distributor in the week. Zero when no shipment.',
  cases_depleted COMMENT 'Sell-out: cases sold from the distributor to retail in the week. Zero when no depletion.',
  stock_build_cases COMMENT 'cases_shipped minus cases_depleted. Positive means the distributor stock grew. Negative means it drew down.'
)
COMMENT 'Weekly sell-in (shipments from Acme Beverages to distributors) vs sell-out (depletions from distributors to retail) per distributor and SKU, in cases. Use to compare what distributors received with what they sold and to see stock build or draw-down. Grain is week x distributor x SKU. For market or brand totals use shipments_metrics and depletions_metrics.'
AS
WITH shp AS (
  SELECT week_start, distributor_id, sku, SUM(cases_shipped) AS cases_shipped
  FROM ${catalog}.${schema}.fact_shipments
  GROUP BY week_start, distributor_id, sku
),
dep AS (
  SELECT week_start, distributor_id, sku, SUM(cases_depleted) AS cases_depleted
  FROM ${catalog}.${schema}.fact_depletions
  GROUP BY week_start, distributor_id, sku
),
keys AS (
  SELECT week_start, distributor_id, sku FROM shp
  UNION
  SELECT week_start, distributor_id, sku FROM dep
)
SELECT k.week_start,
       d.market_id,
       m.market_name,
       k.distributor_id,
       d.distributor_name,
       d.channel,
       k.sku,
       p.product_name,
       p.brand,
       COALESCE(shp.cases_shipped, 0) AS cases_shipped,
       COALESCE(dep.cases_depleted, 0) AS cases_depleted,
       COALESCE(shp.cases_shipped, 0) - COALESCE(dep.cases_depleted, 0) AS stock_build_cases
FROM keys k
LEFT JOIN shp ON shp.week_start = k.week_start AND shp.distributor_id = k.distributor_id AND shp.sku = k.sku
LEFT JOIN dep ON dep.week_start = k.week_start AND dep.distributor_id = k.distributor_id AND dep.sku = k.sku
JOIN ${catalog}.${schema}.dim_distributor d ON d.distributor_id = k.distributor_id
JOIN ${catalog}.${schema}.dim_market m ON m.market_id = d.market_id
JOIN ${catalog}.${schema}.dim_product p ON p.sku = k.sku;

-- 2. Column comments for replenishment_requests (table had none on these columns; status and table comment are untouched).
ALTER TABLE ${catalog}.${schema}.replenishment_requests ALTER COLUMN request_id COMMENT 'Unique identifier of the replenishment request.';
ALTER TABLE ${catalog}.${schema}.replenishment_requests ALTER COLUMN created_at COMMENT 'Timestamp when the request was logged.';
ALTER TABLE ${catalog}.${schema}.replenishment_requests ALTER COLUMN requested_by COMMENT 'Person or agent that raised the request.';
ALTER TABLE ${catalog}.${schema}.replenishment_requests ALTER COLUMN market_id COMMENT 'Market code. Joins dim_market.market_id.';
ALTER TABLE ${catalog}.${schema}.replenishment_requests ALTER COLUMN distributor_id COMMENT 'Distributor code. Joins dim_distributor.distributor_id.';
ALTER TABLE ${catalog}.${schema}.replenishment_requests ALTER COLUMN sku COMMENT 'SKU code. Joins dim_product.sku.';
ALTER TABLE ${catalog}.${schema}.replenishment_requests ALTER COLUMN qty_cases COMMENT 'Requested replenishment quantity in cases.';
ALTER TABLE ${catalog}.${schema}.replenishment_requests ALTER COLUMN reason COMMENT 'Free-text reason for the request, e.g. stock cover below target.';
ALTER TABLE ${catalog}.${schema}.replenishment_requests ALTER COLUMN approved_by COMMENT 'Person who approved the request. Empty until approved.';
