CREATE OR REPLACE FUNCTION ${catalog}.${schema}.stock_cover(p_market STRING, p_sku STRING)
RETURNS TABLE (market_id STRING, distributor_id STRING, distributor_name STRING, channel STRING, sku STRING,
               on_hand_cases DOUBLE, in_transit_cases DOUBLE, avg_weekly_depletion_8w DOUBLE,
               cover_days DOUBLE, target_cover_days INT, status STRING)
COMMENT 'p_market = market code (e.g. TX). p_sku = SKU code (e.g. VT-ORIG-473C) or exact product name. Stock cover in days per distributor for one market and SKU: (on hand + in transit) / average daily depletions of the last 8 weeks, compared with the channel target from the replenishment policy. Use for questions like "how many days of stock do we have of X in market Y".'
RETURN
  WITH dep AS (
    SELECT distributor_id, sku, avg(cases_depleted) AS avg_wk
    FROM ${catalog}.${schema}.fact_depletions
    WHERE market_id = upper(p_market) AND sku  IN (SELECT dp.sku FROM ${catalog}.${schema}.dim_product dp WHERE dp.sku = upper(p_sku) OR lower(dp.product_name) = lower(p_sku))
      AND week_start > (SELECT max(week_start) FROM ${catalog}.${schema}.fact_depletions) - INTERVAL 56 DAYS
    GROUP BY distributor_id, sku)
  SELECT i.market_id, i.distributor_id, d.distributor_name, d.channel, i.sku,
         i.on_hand_cases, i.in_transit_cases, round(dep.avg_wk, 1),
         round((i.on_hand_cases + i.in_transit_cases) / greatest(dep.avg_wk / 7.0, 0.1), 1) AS cover_days,
         CASE d.channel WHEN 'Modern Trade' THEN 21 WHEN 'Traditional Trade' THEN 14 ELSE 10 END,
         CASE WHEN (i.on_hand_cases + i.in_transit_cases) / greatest(dep.avg_wk / 7.0, 0.1) < 7 THEN 'AT RISK'
              WHEN (i.on_hand_cases + i.in_transit_cases) / greatest(dep.avg_wk / 7.0, 0.1)
                   < CASE d.channel WHEN 'Modern Trade' THEN 21 WHEN 'Traditional Trade' THEN 14 ELSE 10 END THEN 'BELOW TARGET'
              ELSE 'OK' END
  FROM ${catalog}.${schema}.fact_inventory i
  JOIN ${catalog}.${schema}.dim_distributor d ON i.distributor_id = d.distributor_id
  JOIN dep ON dep.distributor_id = i.distributor_id AND dep.sku = i.sku
  WHERE i.market_id = upper(p_market) AND i.sku  IN (SELECT dp.sku FROM ${catalog}.${schema}.dim_product dp WHERE dp.sku = upper(p_sku) OR lower(dp.product_name) = lower(p_sku));

CREATE OR REPLACE FUNCTION ${catalog}.${schema}.oos_risk_skus(p_market STRING, p_threshold_days DOUBLE)
RETURNS TABLE (market_id STRING, distributor_id STRING, distributor_name STRING, channel STRING, sku STRING,
               product_name STRING, cover_days DOUBLE, avg_weekly_depletion_8w DOUBLE, weekly_revenue_at_risk_usd DOUBLE)
COMMENT 'Distributor x SKU positions whose stock cover is below p_threshold_days (policy default 7). Pass p_market = NULL or empty string for all markets. Ordered by revenue at risk. Use for out-of-stock risk questions.'
RETURN
  WITH last_wk AS (SELECT max(week_start) AS mx FROM ${catalog}.${schema}.fact_depletions),
  dep AS (
    SELECT f.distributor_id, f.sku, avg(f.cases_depleted) AS avg_wk, avg(f.net_revenue_usd) AS avg_rev
    FROM ${catalog}.${schema}.fact_depletions f, last_wk
    WHERE f.week_start > last_wk.mx - INTERVAL 56 DAYS AND (coalesce(p_market, '') = '' OR f.market_id = upper(p_market))
    GROUP BY f.distributor_id, f.sku)
  SELECT i.market_id, i.distributor_id, d.distributor_name, d.channel, i.sku, p.product_name,
         round((i.on_hand_cases + i.in_transit_cases) / greatest(dep.avg_wk / 7.0, 0.1), 1) AS cover_days,
         round(dep.avg_wk, 1), round(dep.avg_rev, 0)
  FROM ${catalog}.${schema}.fact_inventory i
  JOIN ${catalog}.${schema}.dim_distributor d ON i.distributor_id = d.distributor_id
  JOIN ${catalog}.${schema}.dim_product p ON i.sku = p.sku
  JOIN dep ON dep.distributor_id = i.distributor_id AND dep.sku = i.sku
  WHERE (i.on_hand_cases + i.in_transit_cases) / greatest(dep.avg_wk / 7.0, 0.1) < p_threshold_days
  ORDER BY dep.avg_rev DESC;

CREATE OR REPLACE FUNCTION ${catalog}.${schema}.suggest_replenishment(p_distributor_id STRING, p_sku STRING)
RETURNS TABLE (distributor_id STRING, sku STRING, cover_days DOUBLE, target_cover_days INT,
               recommended_qty_cases DOUBLE, needs_manager_approval BOOLEAN, policy_note STRING)
COMMENT 'p_distributor_id = distributor code (e.g. FL-D2). p_sku = SKU code (e.g. HB-LAGR-355B) or exact product name. Recommended replenishment quantity to bring one distributor x SKU back to its channel target cover, rounded up to the 50-case minimum order. Requests above 500 cases need a supply planning manager. Never creates the request; the agent must get explicit user approval first.'
RETURN
  WITH dep AS (
    SELECT avg(cases_depleted) AS avg_wk FROM ${catalog}.${schema}.fact_depletions
    WHERE distributor_id = upper(p_distributor_id) AND sku  IN (SELECT dp.sku FROM ${catalog}.${schema}.dim_product dp WHERE dp.sku = upper(p_sku) OR lower(dp.product_name) = lower(p_sku))
      AND week_start > (SELECT max(week_start) FROM ${catalog}.${schema}.fact_depletions) - INTERVAL 56 DAYS),
  pos AS (
    SELECT i.distributor_id, i.sku, i.on_hand_cases + i.in_transit_cases AS stock,
           CASE d.channel WHEN 'Modern Trade' THEN 21 WHEN 'Traditional Trade' THEN 14 ELSE 10 END AS tgt
    FROM ${catalog}.${schema}.fact_inventory i JOIN ${catalog}.${schema}.dim_distributor d ON i.distributor_id = d.distributor_id
    WHERE i.distributor_id = upper(p_distributor_id) AND i.sku  IN (SELECT dp.sku FROM ${catalog}.${schema}.dim_product dp WHERE dp.sku = upper(p_sku) OR lower(dp.product_name) = lower(p_sku)))
  SELECT pos.distributor_id, pos.sku, round(pos.stock / greatest(dep.avg_wk / 7.0, 0.1), 1), pos.tgt,
         -- at or above target cover: nothing to ship; below it, round up to the 50-case minimum order
         CASE WHEN pos.tgt * dep.avg_wk / 7.0 - pos.stock <= 0 THEN 0
              ELSE greatest(50, ceil((pos.tgt * dep.avg_wk / 7.0 - pos.stock) / 50) * 50) END AS qty,
         CASE WHEN pos.tgt * dep.avg_wk / 7.0 - pos.stock <= 0 THEN false
              ELSE greatest(50, ceil((pos.tgt * dep.avg_wk / 7.0 - pos.stock) / 50) * 50) > 500 END,
         'Target cover by channel: Modern Trade 21d, Traditional Trade 14d, On-Premise 10d. Minimum order 50 cases. Above 500 cases needs manager approval.'
  FROM pos, dep;

CREATE OR REPLACE FUNCTION ${catalog}.${schema}.promo_performance(p_market STRING, p_status STRING)
RETURNS TABLE (promo_id STRING, market_id STRING, sku STRING, product_name STRING, start_date DATE, end_date DATE,
               discount_pct DOUBLE, mechanic STRING, uplift_forecast_pct DOUBLE, uplift_actual_pct DOUBLE,
               gap_vs_forecast_pct DOUBLE, status STRING)
COMMENT 'Trade promotions with forecast vs measured uplift. p_status: Active, Completed (case-insensitive), or NULL/empty for all; p_market NULL/empty for all markets. Ordered by the largest shortfall against forecast.'
RETURN
  SELECT pr.promo_id, pr.market_id, pr.sku, p.product_name, pr.start_date, pr.end_date, pr.discount_pct, pr.mechanic,
         pr.uplift_forecast_pct, pr.uplift_actual_pct, round(pr.uplift_actual_pct - pr.uplift_forecast_pct, 3), pr.status
  FROM ${catalog}.${schema}.fact_promotions pr JOIN ${catalog}.${schema}.dim_product p ON pr.sku = p.sku
  WHERE (coalesce(p_market, '') = '' OR pr.market_id = upper(p_market)) AND (coalesce(p_status, '') = '' OR lower(pr.status) = lower(p_status))
  ORDER BY pr.uplift_actual_pct - pr.uplift_forecast_pct ASC;

CREATE OR REPLACE FUNCTION ${catalog}.${schema}.promo_by_distributor(p_promo_id STRING)
RETURNS TABLE (promo_id STRING, distributor_id STRING, distributor_name STRING, channel STRING,
               pre_promo_avg_cases DOUBLE, in_promo_avg_cases DOUBLE, uplift_pct DOUBLE)
COMMENT 'Measured uplift of one promotion per distributor: average weekly depletions during the promo vs the 8 weeks before it. Use to find where an in-flight promo is under-delivering.'
RETURN
  WITH pr AS (SELECT * FROM ${catalog}.${schema}.fact_promotions WHERE promo_id = upper(p_promo_id)),
  dep AS (
    SELECT f.distributor_id,
           avg(CASE WHEN f.week_start < pr.start_date AND f.week_start >= pr.start_date - INTERVAL 56 DAYS THEN f.cases_depleted END) AS pre,
           avg(CASE WHEN f.week_start >= pr.start_date AND f.week_start <= pr.end_date THEN f.cases_depleted END) AS dur
    FROM ${catalog}.${schema}.fact_depletions f JOIN pr ON f.market_id = pr.market_id AND f.sku = pr.sku
    GROUP BY f.distributor_id)
  SELECT p_promo_id, dep.distributor_id, d.distributor_name, d.channel, round(dep.pre, 1), round(dep.dur, 1),
         round(dep.dur / dep.pre - 1, 3)
  FROM dep JOIN ${catalog}.${schema}.dim_distributor d ON dep.distributor_id = d.distributor_id
  ORDER BY dep.dur / dep.pre - 1 ASC
;

CREATE OR REPLACE FUNCTION ${catalog}.${schema}.distributor_snapshot(p_distributor_id STRING)
RETURNS TABLE (section STRING, sku STRING, product_name STRING, status STRING, value DOUBLE, target DOUBLE,
               weekly_revenue_usd DOUBLE, note STRING)
COMMENT 'p_distributor_id = distributor code (e.g. NY-D2). One-call health check of a distributor for "what needs attention at X": section summary (position counts by stock status; weekly_revenue_usd = weekly revenue of the positions below target), stock (positions below target cover: value = cover days, target = channel target), promo (active promotions: value = measured uplift, target = forecast uplift), sell_out (up to 5 SKUs whose last-4-week depletion change vs the prior 4 weeks differs from the market trend by 5 points or more: value = distributor change, target = market change, note = sell-in vs sell-out). Ordered by section, then severity and revenue.'
RETURN
  WITH cov AS (SELECT * FROM ${catalog}.${schema}.v_stock_cover WHERE distributor_id = upper(p_distributor_id)),
  mx AS (SELECT max(week_start) AS w FROM ${catalog}.${schema}.fact_depletions),
  dep AS (
    SELECT f.sku,
           avg(CASE WHEN f.week_start > mx.w - INTERVAL 28 DAYS THEN f.cases_depleted END) AS last4,
           avg(CASE WHEN f.week_start <= mx.w - INTERVAL 28 DAYS THEN f.cases_depleted END) AS prev4,
           avg(CASE WHEN f.week_start > mx.w - INTERVAL 28 DAYS THEN f.net_revenue_usd END) AS rev4
    FROM ${catalog}.${schema}.fact_depletions f, mx
    WHERE f.distributor_id = upper(p_distributor_id) AND f.week_start > mx.w - INTERVAL 56 DAYS
    GROUP BY f.sku),
  mkt AS (
    SELECT f.sku,
           avg(CASE WHEN f.week_start > mx.w - INTERVAL 28 DAYS THEN f.cases_depleted END)
             / avg(CASE WHEN f.week_start <= mx.w - INTERVAL 28 DAYS THEN f.cases_depleted END) - 1 AS chg
    FROM ${catalog}.${schema}.fact_depletions f JOIN ${catalog}.${schema}.dim_distributor dd ON dd.market_id = f.market_id, mx
    WHERE dd.distributor_id = upper(p_distributor_id) AND f.week_start > mx.w - INTERVAL 56 DAYS
    GROUP BY f.sku),
  shp AS (
    SELECT s.sku, avg(s.cases_shipped) AS in4
    FROM ${catalog}.${schema}.fact_shipments s, mx
    WHERE s.distributor_id = upper(p_distributor_id) AND s.week_start > mx.w - INTERVAL 28 DAYS
    GROUP BY s.sku),
  items AS (
    SELECT 0 AS ord, 'summary' AS section, CAST(NULL AS STRING) AS sku, CAST(NULL AS STRING) AS product_name,
           concat(sum(CASE WHEN status = 'AT RISK' THEN 1 ELSE 0 END), ' AT RISK, ', sum(CASE WHEN status = 'BELOW TARGET' THEN 1 ELSE 0 END),
                  ' BELOW TARGET, ', sum(CASE WHEN status = 'OK' THEN 1 ELSE 0 END), ' OK') AS status,
           CAST(count(*) AS DOUBLE) AS value, CAST(NULL AS DOUBLE) AS target, sum(CASE WHEN status <> 'OK' THEN weekly_revenue_at_risk_usd ELSE 0 END) AS weekly_revenue_usd,
           concat(max(distributor_name), ' (', max(channel), '), ', count(*), ' SKU positions; total on hand ', CAST(round(sum(on_hand_cases)) AS BIGINT),
                  ' cases, in transit ', CAST(round(sum(in_transit_cases)) AS BIGINT), ' cases; weekly revenue of positions below target ',
                  CAST(round(sum(CASE WHEN status <> 'OK' THEN weekly_revenue_at_risk_usd ELSE 0 END)) AS BIGINT), ' USD') AS note,
           0.0 AS sev
    FROM cov
    UNION ALL
    SELECT 1, 'stock', sku, product_name, status, cover_days, target_cover_days, weekly_revenue_at_risk_usd,
           concat('on hand ', CAST(round(on_hand_cases) AS BIGINT), ', in transit ', CAST(round(in_transit_cases) AS BIGINT),
                  ', avg weekly depletion ', avg_weekly_depletion_8w),
           CASE status WHEN 'AT RISK' THEN 0 ELSE 1 END
    FROM cov WHERE status <> 'OK'
    UNION ALL
    SELECT 2, 'promo', u.sku, p.product_name, CASE WHEN u.gap_vs_forecast_pct < 0 THEN 'BELOW FORECAST' ELSE 'ON OR ABOVE FORECAST' END,
           u.uplift_actual_pct, u.uplift_forecast_pct, CAST(NULL AS DOUBLE),
           concat(u.promo_id, ' ', u.mechanic, ', discount ', u.discount_pct, ', gap vs forecast ', u.gap_vs_forecast_pct), u.gap_vs_forecast_pct
    FROM ${catalog}.${schema}.v_promo_distributor_uplift u JOIN ${catalog}.${schema}.dim_product p ON p.sku = u.sku
    WHERE u.distributor_id = upper(p_distributor_id) AND lower(u.status) = 'active'
    UNION ALL
    SELECT 3, 'sell_out', sku, product_name, status, value, target, weekly_revenue_usd, note, sev FROM (
      SELECT dep.sku, p.product_name,
             CASE WHEN dep.last4 / dep.prev4 - 1 - mkt.chg <= -0.05 THEN 'BELOW MARKET TREND'
                  WHEN dep.last4 / dep.prev4 - 1 - mkt.chg >= 0.05 THEN 'ABOVE MARKET TREND' ELSE 'IN LINE WITH MARKET' END AS status,
             round(dep.last4 / dep.prev4 - 1, 3) AS value, round(mkt.chg, 3) AS target, round(dep.rev4, 0) AS weekly_revenue_usd,
             concat('sell-out avg ', round(dep.last4, 1), ' vs ', round(dep.prev4, 1), ' cases/week (last vs prior 4 weeks); market change ',
                    round(mkt.chg, 3), '; sell-in avg ', round(coalesce(shp.in4, 0), 1), ' cases/week (last 4 weeks)') AS note,
             dep.last4 / dep.prev4 - 1 - mkt.chg AS sev,
             row_number() OVER (ORDER BY abs(dep.last4 / dep.prev4 - 1 - mkt.chg) DESC) AS rn
      FROM dep JOIN ${catalog}.${schema}.dim_product p ON p.sku = dep.sku JOIN mkt ON mkt.sku = dep.sku LEFT JOIN shp ON shp.sku = dep.sku
      WHERE dep.prev4 > 0 AND abs(dep.last4 / dep.prev4 - 1 - mkt.chg) >= 0.05)
    WHERE rn <= 5)
  SELECT section, sku, product_name, status, value, target, weekly_revenue_usd, note
  FROM items
  ORDER BY ord, sev, weekly_revenue_usd DESC NULLS LAST
