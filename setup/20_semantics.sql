ALTER TABLE ${catalog}.${schema}.fact_depletions ALTER COLUMN cases_depleted COMMENT 'Cases sold out from the distributor to retail customers in the week (depletions = sell-out).';
ALTER TABLE ${catalog}.${schema}.fact_depletions ALTER COLUMN net_revenue_usd COMMENT 'Net revenue in USD after promotional discount.';
ALTER TABLE ${catalog}.${schema}.fact_depletions ALTER COLUMN on_promo COMMENT 'True when a trade promotion was active for this market and SKU in the week.';
ALTER TABLE ${catalog}.${schema}.fact_shipments ALTER COLUMN cases_shipped COMMENT 'Cases shipped from Acme Beverages to the distributor in the week (shipments = sell-in).';
ALTER TABLE ${catalog}.${schema}.fact_inventory ALTER COLUMN on_hand_cases COMMENT 'Cases on hand at the distributor on the snapshot date.';
ALTER TABLE ${catalog}.${schema}.fact_inventory ALTER COLUMN in_transit_cases COMMENT 'Cases shipped but not yet received by the distributor.';
ALTER TABLE ${catalog}.${schema}.fact_promotions ALTER COLUMN uplift_forecast_pct COMMENT 'Planned volume uplift vs baseline, as a fraction (0.45 = +45%).';
ALTER TABLE ${catalog}.${schema}.fact_promotions ALTER COLUMN uplift_actual_pct COMMENT 'Measured volume uplift vs the 8 pre-promo weeks, as a fraction. For Active promos it is measured to date.';
ALTER TABLE ${catalog}.${schema}.fact_promotions ALTER COLUMN status COMMENT 'Completed or Active.';
CREATE OR REPLACE VIEW ${catalog}.${schema}.depletions_metrics
WITH METRICS
LANGUAGE YAML
COMMENT 'Governed sell-out metrics for Acme Beverages: depletions in cases and net revenue by week, market, distributor, channel, brand and SKU.'
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
    comment: Monday of the depletion week.
  - name: market_name
    expr: market.market_name
  - name: region
    expr: market.region
  - name: distributor_name
    expr: distributor.distributor_name
  - name: channel
    expr: distributor.channel
  - name: brand
    expr: product.brand
  - name: category
    expr: product.category
  - name: sku
    expr: source.sku
  - name: product_name
    expr: product.product_name
  - name: on_promo
    expr: source.on_promo
measures:
  - name: cases_depleted
    expr: SUM(source.cases_depleted)
    comment: Total depletions (sell-out) in cases.
  - name: net_revenue_usd
    expr: SUM(source.net_revenue_usd)
    comment: Net revenue in USD after promotional discount.
  - name: promo_share_of_cases
    expr: SUM(CASE WHEN source.on_promo THEN source.cases_depleted ELSE 0 END) / NULLIF(SUM(source.cases_depleted), 0)
    comment: Share of depleted cases sold on promotion.
  - name: avg_net_price_per_case_usd
    expr: SUM(source.net_revenue_usd) / NULLIF(SUM(source.cases_depleted), 0)
$$;
SELECT region, MEASURE(cases_depleted) AS cases, MEASURE(net_revenue_usd) AS revenue FROM ${catalog}.${schema}.depletions_metrics WHERE week_start > (SELECT max(week_start) FROM ${catalog}.${schema}.fact_depletions) - INTERVAL 28 DAYS GROUP BY region ORDER BY revenue DESC
