"""Read API: identity, overview panels (UC functions on the warehouse, as the user) and replenishment requests (Lakebase, group role)."""
import logging
import re

import psycopg
from fastapi import APIRouter, HTTPException, Request

from server import auth, warehouse
from server import settings as S
from server.identity import clean, current_user, require_role

logger = logging.getLogger(__name__)
router = APIRouter()

MARKET = re.compile(r"^[A-Z]{2}$")
CODE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")  # promo / distributor / SKU codes (no format assumptions)
STATUSES = ("proposed", "approved", "rejected", "withdrawn")


def _market(v: str | None) -> str | None:
    if v in (None, ""):
        return None
    if not MARKET.match(v):
        raise HTTPException(400, "Invalid market code")
    return v


@router.get("/health")
def health():
    # liveness only: no version, config or turn counters (bridge.health() stays available to operators in code and logs)
    return {"status": "ok"}


@router.get("/me")
def me(request: Request):
    u = current_user(request)
    return {"email": u.email, "roles": [{"role": r, **S.ROLES[r]} for r in u.roles if r in S.ROLES],  # the manager group is a flag, not a role
            "can_approve": u.can_approve, "can_propose": u.can_propose, "is_manager": u.is_manager,
            "self_approval": S.ALLOW_SELF_APPROVAL, "self_approval_ignored": S.SELF_APPROVAL_IGNORED,
            "manager_threshold_cases": S.MANAGER_THRESHOLD_CASES,
            "agent_mode": S.AGENT_MODE, "local_dev": S.LOCAL_DEV}


def _uc(request: Request, sql: str, params: dict):
    u = current_user(request)
    try:
        return warehouse.query(u.client(), sql, params)
    except Exception as e:
        msg = str(e)
        if "PERMISSION" in msg.upper() or "403" in msg:
            raise HTTPException(403, "You don't have access to this data in Unity Catalog")
        logger.error("warehouse query failed: %s", clean(msg))
        raise HTTPException(502, "Data service unavailable")


@router.get("/overview/oos")
def overview_oos(request: Request, market: str | None = None, threshold_days: float = S.OOS_THRESHOLD_DAYS):
    if not 0 < threshold_days <= 60:
        raise HTTPException(400, "threshold_days must be between 0 and 60")
    rows = _uc(request, f"SELECT * FROM {warehouse.fn('oos_risk_skus')}(:market, :threshold) ORDER BY cover_days ASC LIMIT 50",
               {"market": _market(market), "threshold": float(threshold_days)})
    return {"threshold_days": threshold_days, "rows": rows}


@router.get("/overview/promo")
def overview_promo(request: Request, promo_id: str | None = None):
    # default = the active promotion with the largest shortfall (promo_performance orders by it)
    if promo_id is not None and not CODE.match(promo_id):
        raise HTTPException(400, "Invalid promo id")
    active = _uc(request, f"SELECT * FROM {warehouse.fn('promo_performance')}(NULL, 'Active') LIMIT 20", {})
    promo_id = promo_id or (active[0]["promo_id"] if active else None)
    if promo_id is None:
        return {"promo_id": None, "selected": None, "active": [], "distributors": []}
    dist = _uc(request, f"SELECT * FROM {warehouse.fn('promo_by_distributor')}(:promo_id) ORDER BY uplift_pct ASC",
               {"promo_id": promo_id})
    selected = next((p for p in active if p["promo_id"] == promo_id), None)
    return {"promo_id": promo_id, "selected": selected, "active": active, "distributors": dist}


@router.get("/requests")
def requests_list(request: Request, status: str | None = None, market: str | None = None, limit: int = 50):
    from memory.pgretry import retry_once
    from memory.replenishment import list_requests
    if status not in (None, "", *STATUSES):
        raise HTTPException(400, "Invalid status")
    market = _market(market)
    u = current_user(request)
    role = require_role(u)
    try:
        def read():
            with u.connection(role) as conn:
                return list_requests(conn, status=status or None, limit=min(max(limit, 1), 100), market_id=market or None)
        rows = retry_once(read)
    except HTTPException:
        raise
    except psycopg.errors.InsufficientPrivilege:
        raise HTTPException(403, "Not permitted for this role")
    except auth.GovernanceError:
        raise HTTPException(503, "Connection identity bypasses row-level security; refusing to serve data")
    except Exception as e:
        logger.error("requests query failed: %s", clean(str(e)))
        raise HTTPException(502, "Data service unavailable")
    return {"rows": rows}
