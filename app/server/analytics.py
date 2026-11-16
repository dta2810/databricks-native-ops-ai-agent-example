"""Executive page + market/distributor 360: governed metric views, views and UC functions on the warehouse, as the user (OBO).
Numbers come from the same UC functions the agent and Genie use (oos_risk_skus, promo_performance, promo_by_distributor) or the
metric views (depletions_metrics). AI insights: llm_fast via the Unity AI Gateway over the already-computed JSON only.
"""
import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import requests
from fastapi import APIRouter, HTTPException, Request

from server import settings as S
from server.ratelimit import TokenBucket
from server import warehouse
from server.identity import User, clean, current_user, require_role

logger = logging.getLogger(__name__)
router = APIRouter()

MARKET = re.compile(r"^[A-Z]{2}$")
CODE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
T = lambda name: f"{S.CATALOG}.{S.SCHEMA}.{name}"  # noqa: E731
_pool = ThreadPoolExecutor(max_workers=8)


def _q(user: User, sql: str, params: dict | None = None):
    try:
        return warehouse.query(user.client(), sql, params or {})
    except Exception as e:
        msg = str(e)
        if "PERMISSION" in msg.upper() or "403" in msg:
            raise HTTPException(403, "You don't have access to this data in Unity Catalog")
        logger.error("warehouse query failed: %s", clean(msg))
        raise HTTPException(502, "Data service unavailable")


def _par(*calls):
    # run independent warehouse queries in parallel; first failure wins
    futs = [_pool.submit(f, *a) for f, *a in calls]
    return [f.result() for f in futs]


def _market(v):
    if v in (None, ""):
        return None
    if not MARKET.match(v):
        raise HTTPException(400, "Invalid market code")
    return v


def _code(v, what):
    if not CODE.match(v or ""):
        raise HTTPException(400, f"Invalid {what}")
    return v


def _pct(cur, base):
    return None if not base else cur / base - 1


# ---------- reference ----------

_ref_cache: dict = {}


@router.get("/markets")
def markets(request: Request):
    u = current_user(request)
    hit = _ref_cache.get(u.email)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    m, d = _par((_q, u, f"SELECT market_id, market_name, region FROM {T('dim_market')} ORDER BY market_name"),
                (_q, u, f"SELECT distributor_id, distributor_name, market_id, channel FROM {T('dim_distributor')} ORDER BY distributor_id"))
    out = {"markets": m, "distributors": d}
    _ref_cache[u.email] = (time.time(), out)
    return out


# ---------- executive ----------

def _weekly_depletions(u: User):
    return _q(u, f"""SELECT week_start, MEASURE(net_revenue_usd) AS net_revenue_usd, MEASURE(cases_depleted) AS cases_depleted
                     FROM {T('depletions_metrics')} GROUP BY week_start ORDER BY week_start""")


def _window(rows, end: date, weeks: int):
    start = end - timedelta(weeks=weeks - 1)
    sel = [r for r in rows if start <= date.fromisoformat(r["week_start"][:10]) <= end]
    return sum(r["net_revenue_usd"] or 0 for r in sel), sum(r["cases_depleted"] or 0 for r in sel), len(sel)


def _pending_requests(u: User):
    from memory.replenishment import list_requests
    try:
        with u.connection(require_role(u)) as conn:
            return len(list_requests(conn, status="proposed", limit=100))
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("pending requests unavailable: %s", clean(str(e)))
        return None


_kpi_cache: dict = {}
_KPI_TTL = 60


def _kpis(u: User):
    # the insights call right after the KPI tiles reuses the same snapshot
    hit = _kpi_cache.get(u.email)
    if hit and time.time() - hit[0] < _KPI_TTL:
        return hit[1]
    out = _kpis_fresh(u)
    _kpi_cache[u.email] = (time.time(), out)
    return out


def _kpis_fresh(u: User):
    weekly, oos, promos = _par(
        (_weekly_depletions, u),
        (_q, u, f"SELECT * FROM {warehouse.fn('oos_risk_skus')}(NULL, :th)", {"th": float(S.OOS_THRESHOLD_DAYS)}),
        (_q, u, f"SELECT * FROM {warehouse.fn('promo_performance')}(NULL, 'Active')"),
    )
    pending = _pending_requests(u)
    if not weekly:
        raise HTTPException(502, "No depletion data")
    end = date.fromisoformat(weekly[-1]["week_start"][:10])
    rev, cases, _ = _window(weekly, end, 4)
    prev_rev, prev_cases, _ = _window(weekly, end - timedelta(weeks=4), 4)
    ly_rev, ly_cases, ly_n = _window(weekly, end - timedelta(weeks=52), 4)
    risk = sum(r["weekly_revenue_at_risk_usd"] or 0 for r in oos)
    critical = [r for r in oos if (r["cover_days"] or 0) < S.OOS_THRESHOLD_DAYS / 2]
    worst = min(promos, key=lambda p: p["gap_vs_forecast_pct"]) if promos else None
    tiles = [
        {"id": "net_revenue_4w", "label": "Net revenue, last 4 weeks", "value": rev, "format": "currency",
         "delta": _pct(rev, prev_rev), "delta_label": "vs prior 4 weeks", "tone": "neutral"},
        {"id": "depletions_4w", "label": "Depletions, last 4 weeks", "value": cases, "format": "cases",
         "delta": _pct(cases, ly_cases) if ly_n == 4 else _pct(cases, prev_cases),
         "delta_label": "vs same weeks last year" if ly_n == 4 else "vs prior 4 weeks", "tone": "neutral"},
        {"id": "oos_positions", "label": "Positions at out-of-stock risk", "value": len(oos), "format": "count",
         "subtitle": f"below {S.OOS_THRESHOLD_DAYS:g} days of cover · {len(critical)} under {S.OOS_THRESHOLD_DAYS / 2:g} days",
         "tone": "alert" if critical else ("warn" if oos else "good")},
        {"id": "revenue_at_risk", "label": "Weekly revenue at risk", "value": risk, "format": "currency",
         "subtitle": "from positions at out-of-stock risk", "tone": "warn" if risk else "good"},
        {"id": "promos_active", "label": "In-flight promotions", "value": len(promos), "format": "count",
         "subtitle": (f"worst: {worst['promo_id']} {worst['uplift_actual_pct'] * 100:+.0f}% vs {worst['uplift_forecast_pct'] * 100:+.0f}% forecast"
                      if worst else "none active"),
         "tone": "alert" if worst and worst["gap_vs_forecast_pct"] < -0.2 else "neutral"},
        {"id": "pending_requests", "label": "Requests waiting for approval", "value": pending, "format": "count",
         "subtitle": "replenishment requests proposed by the agent", "tone": "warn" if pending else "neutral"},
    ]
    return {"as_of_week": str(end), "tiles": tiles}, oos, promos


@router.get("/exec/kpis")
def exec_kpis(request: Request):
    u = current_user(request)
    out, _, _ = _kpis(u)
    return out


@router.get("/exec/trend")
def exec_trend(request: Request, market: str | None = None, weeks: int = 26):
    u = current_user(request)
    weeks = min(max(weeks, 4), 78)
    rows = _q(u, f"""SELECT week_start, SUM(cases_shipped) AS sell_in_cases, SUM(cases_depleted) AS sell_out_cases
                     FROM {T('v_sell_in_out_weekly')}
                     WHERE (:market IS NULL OR market_id = :market)
                       AND week_start > (SELECT max(week_start) FROM {T('v_sell_in_out_weekly')}) - make_interval(0, 0, :weeks)
                     GROUP BY week_start ORDER BY week_start""", {"market": _market(market), "weeks": int(weeks)})
    return {"market": market or None, "rows": rows}


def _alerts(oos, promo_rows, pending):
    out = []
    th = S.OOS_THRESHOLD_DAYS
    for r in sorted(oos, key=lambda r: (r["cover_days"], -(r["weekly_revenue_at_risk_usd"] or 0)))[:4]:
        out.append({"id": f"oos-{r['distributor_id']}-{r['sku']}", "category": "oos",
                    "severity": "alert" if r["cover_days"] < th / 2 else "warn",
                    "title": f"{r['product_name']} at {r['cover_days']:g} days of cover",
                    "body": f"{r['distributor_id']} {r['distributor_name']} ({r['channel']}), ${r['weekly_revenue_at_risk_usd']:,.0f} weekly revenue at risk.",
                    "market": r["market_id"],
                    "ask": f"What should we send to {r['distributor_id']} for {r['product_name']} ({r['sku']})?"})
    for p in [p for p in promo_rows if p["gap_vs_forecast_pct"] is not None and p["gap_vs_forecast_pct"] < -0.2][:3]:
        out.append({"id": f"promo-{p['promo_id']}-{p['distributor_id']}", "category": "promo", "severity": "alert" if p["uplift_actual_pct"] < 0 else "warn",
                    "title": f"{p['promo_id']} under forecast at {p['distributor_id']}",
                    "body": f"{p['distributor_name']}: {p['uplift_actual_pct'] * 100:+.0f}% measured vs {p['uplift_forecast_pct'] * 100:+.0f}% forecast ({p['mechanic']}).",
                    "market": p["market_id"], "ask": f"Why is promo {p['promo_id']} underperforming at {p['distributor_id']}?"})
    if pending:
        out.append({"id": "requests-pending", "category": "request", "severity": "info",
                    "title": f"{pending} replenishment request{'s' if pending != 1 else ''} waiting for approval",
                    "body": "Proposed by the agent; nothing ships until an approver decides.", "market": None, "ask": None})
    order = {"alert": 0, "warn": 1, "info": 2, "good": 3}
    return sorted(out, key=lambda a: order[a["severity"]])


def _promo_gaps(u: User):
    return _q(u, f"""SELECT promo_id, market_id, mechanic, distributor_id, distributor_name, channel, uplift_forecast_pct,
                            uplift_actual_pct, gap_vs_forecast_pct
                     FROM {T('v_promo_distributor_uplift')} WHERE status = 'Active' ORDER BY gap_vs_forecast_pct ASC LIMIT 20""")


@router.get("/exec/alerts")
def exec_alerts(request: Request):
    u = current_user(request)
    oos, gaps = _par((_q, u, f"SELECT * FROM {warehouse.fn('oos_risk_skus')}(NULL, :th)", {"th": float(S.OOS_THRESHOLD_DAYS)}), (_promo_gaps, u))
    return {"alerts": _alerts(oos, gaps, _pending_requests(u))}


# ---------- AI insights (llm_fast) ----------

_insights_cache: dict = {}
_INSIGHTS_TTL = S.INSIGHTS_TTL_S
INSIGHT_BULLETS = 3


def _llm_fast(u: User, payload: dict) -> str:
    from server.prompts import demo_cases, render, template
    cases = demo_cases()
    system = render(template("insights.md"), company=cases.get("company", ""), currency=cases.get("currency", "USD"),
                    as_of_week=payload["as_of_week"], bullets=INSIGHT_BULLETS)
    w = u.client()
    from agent.gateway_tags import header
    headers = w.config.authenticate() | {"Content-Type": "application/json"} | header("insights")
    r = requests.post(f"{w.config.host}/ai-gateway/mlflow/v1/chat/completions", headers=headers, timeout=30, json={
        "model": S.LLM_FAST, "max_tokens": S.INSIGHTS_MAX_TOKENS, "temperature": 0.2,
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": json.dumps(payload, default=str, separators=(",", ":"))}]})
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content.strip()


def _bullets(text):
    return [b.lstrip("-• ").strip() for b in (text or "").splitlines() if b.strip()][:INSIGHT_BULLETS]


insights_limiter = TokenBucket(float(__import__("os").environ.get("BEV_INSIGHTS_RATE_PER_MIN", "4")))


@router.get("/exec/insights")
def exec_insights(request: Request, refresh: bool = False):
    u = current_user(request)
    kpis, oos, _ = _kpis(u)
    alerts = _alerts(oos, _promo_gaps(u), next((t["value"] for t in kpis["tiles"] if t["id"] == "pending_requests"), None))
    payload = {"as_of_week": kpis["as_of_week"],
               "kpis": [{k: t.get(k) for k in ("label", "value", "delta", "delta_label", "subtitle")} for t in kpis["tiles"]],
               "alerts": [{k: a[k] for k in ("severity", "title", "body")} for a in alerts]}
    snap = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
    key = (u.email, "exec", snap)
    hit = _insights_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < _INSIGHTS_TTL:
        return hit[1] | {"cached": True}
    # every miss/refresh is an LLM call: bound them per user (a cached answer is still served when limited)
    if not insights_limiter.allow(u.email.lower()):
        if hit:
            return hit[1] | {"cached": True}
        raise HTTPException(429, "Too many insight refreshes in a short time. Wait a minute and try again.")
    t0 = time.time()
    try:
        text = _llm_fast(u, payload)
    except Exception as e:
        logger.error("insights failed: %s", clean(str(e)))
        raise HTTPException(502, "Insights are unavailable right now")
    from server.grounding import grounded_bullets
    bullets, dropped = grounded_bullets(_bullets(text), payload)
    if not bullets:
        # regenerate once when every bullet carried an ungrounded figure
        try:
            bullets, dropped = grounded_bullets(_bullets(_llm_fast(u, payload)), payload)
        except Exception as e:
            logger.error("insights retry failed: %s", clean(str(e)))
    if dropped:
        logger.warning("insights dropped %d ungrounded bullet(s)", len(dropped))
    out = {"bullets": bullets, "dropped_ungrounded": len(dropped), "model": S.LLM_FAST, "as_of_week": kpis["as_of_week"],
           "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "latency_ms": int((time.time() - t0) * 1000)}
    for k in [k for k, v in _insights_cache.items() if time.time() - v[0] > _INSIGHTS_TTL]:
        _insights_cache.pop(k, None)
    _insights_cache[key] = (time.time(), out)
    return out | {"cached": False}


# ---------- distributor 360 ----------

@router.get("/distributors/{distributor_id}/summary")
def distributor_summary(distributor_id: str, request: Request):
    u = current_user(request)
    d = _code(distributor_id, "distributor id")
    head, cover, promos = _par(
        (_q, u, f"""SELECT d.distributor_id, d.distributor_name, d.channel, d.market_id, m.market_name, m.region
                    FROM {T('dim_distributor')} d JOIN {T('dim_market')} m ON m.market_id = d.market_id
                    WHERE d.distributor_id = :d""", {"d": d}),
        (_q, u, f"""SELECT sku, product_name, brand, on_hand_cases, in_transit_cases, avg_weekly_depletion_8w, cover_days,
                           target_cover_days, weekly_revenue_at_risk_usd, status
                    FROM {T('v_stock_cover')} WHERE distributor_id = :d ORDER BY cover_days ASC""", {"d": d}),
        (_q, u, f"""SELECT promo_id, sku, mechanic, discount_pct, uplift_forecast_pct, uplift_actual_pct, gap_vs_forecast_pct,
                           pre_promo_avg_cases, in_promo_avg_cases
                    FROM {T('v_promo_distributor_uplift')} WHERE distributor_id = :d AND status = 'Active'
                    ORDER BY gap_vs_forecast_pct ASC""", {"d": d}),
    )
    if not head:
        raise HTTPException(404, "Distributor not found")
    return {"distributor": head[0], "cover": cover, "promos": promos,
            "counts": {"at_risk": sum(c["status"] == "AT RISK" for c in cover), "below_target": sum(c["status"] == "BELOW TARGET" for c in cover),
                       "skus": len(cover)}}


@router.get("/distributors/{distributor_id}/trend")
def distributor_trend(distributor_id: str, request: Request, sku: str | None = None, weeks: int = 26):
    u = current_user(request)
    d = _code(distributor_id, "distributor id")
    s = _code(sku, "SKU") if sku else None
    weeks = min(max(weeks, 4), 78)
    rows = _q(u, f"""SELECT week_start, SUM(cases_shipped) AS sell_in_cases, SUM(cases_depleted) AS sell_out_cases
                     FROM {T('v_sell_in_out_weekly')}
                     WHERE distributor_id = :d AND (:sku IS NULL OR sku = :sku)
                       AND week_start > (SELECT max(week_start) FROM {T('v_sell_in_out_weekly')}) - make_interval(0, 0, :weeks)
                     GROUP BY week_start ORDER BY week_start""", {"d": d, "sku": s, "weeks": int(weeks)})
    return {"distributor_id": d, "sku": s, "rows": rows}


@router.get("/distributors/{distributor_id}/activity")
def distributor_activity(distributor_id: str, request: Request):
    from memory.decisions import recall_decisions
    from memory.replenishment import list_requests
    u = current_user(request)
    d = _code(distributor_id, "distributor id")
    try:
        with u.connection(require_role(u)) as conn:
            reqs = list_requests(conn, distributor_id=d, limit=20)
            decs = recall_decisions(conn, u.email, distributor_id=d, top_k=10)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("activity query failed: %s", clean(str(e)))
        raise HTTPException(502, "Data service unavailable")
    return {"requests": reqs, "decisions": [{k: v for k, v in r.items() if k != "embedding"} for r in decs]}
