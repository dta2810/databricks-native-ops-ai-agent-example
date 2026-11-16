SELECT * FROM ${catalog}.${schema}.stock_cover('TX', 'VT-ORIG-473C');
SELECT market_id, distributor_id, sku, cover_days, weekly_revenue_at_risk_usd FROM ${catalog}.${schema}.oos_risk_skus(NULL, 7) LIMIT 8;
SELECT * FROM ${catalog}.${schema}.suggest_replenishment('FL-D2', 'HB-LAGR-355B');
SELECT promo_id, market_id, sku, uplift_forecast_pct, uplift_actual_pct, status FROM ${catalog}.${schema}.promo_performance(NULL, 'Active');
SELECT * FROM ${catalog}.${schema}.promo_by_distributor('PR-0422')
