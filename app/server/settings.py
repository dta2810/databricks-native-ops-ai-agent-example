"""App settings. Every id/name comes from env: bundle vars in Apps (resources/app.yml), targets/<BEV_TARGET>.env in local dev
(same BEV_* names as targets/example.env and agent/context.py). Required values fail fast at startup; no ids in code.
"""
import logging
import os
import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]
# deployed: deploy.py stages config/, memory/, rag/, agent/ into the app folder; locally they live at the repo root
ROOT = _APP if (_APP / "config").is_dir() else _APP.parent
_IDENT = re.compile(r"^[A-Za-z0-9_]+$")

IN_APPS = bool(os.environ.get("DATABRICKS_APP_NAME"))
LOCAL_DEV = os.environ.get("LOCAL_DEV", "false").lower() == "true"
if LOCAL_DEV and IN_APPS:
    raise RuntimeError("LOCAL_DEV is unsafe inside Databricks Apps; refusing to start")
if LOCAL_DEV:
    # local dev reads the same target files as the setup scripts (targets/<BEV_TARGET>.env + .resolved.env)
    _t = os.environ.get("BEV_TARGET", "")
    _base = ROOT if (ROOT / "targets").is_dir() else ROOT.parent  # ROOT is app/ once deploy.py build staged config/
    for _env in ([_base / "targets" / f"{_t}.env", _base / "targets" / f"{_t}.resolved.env"] if _t else []):
        if not _env.exists():
            continue
        for _line in _env.read_text().splitlines():
            _line = _line.split("#", 1)[0].strip()
            if "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())


def _req(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v or v.startswith("<"):
        raise RuntimeError(f"{name} is required (bundle var in Apps, targets/<BEV_TARGET>.env locally)")
    return v


def _ident(name: str) -> str:
    # interpolated into SQL identifiers: plain names only
    v = _req(name)
    if not _IDENT.match(v):
        raise RuntimeError(f"{name} must be a plain identifier")
    return v


CATALOG = _ident("BEV_CATALOG")
SCHEMA = _ident("BEV_SCHEMA")
WAREHOUSE_ID = _req("BEV_WAREHOUSE_ID")

# Lakebase: full endpoint path, or built from project/branch/endpoint ids (same vars as setup/lakebase_conn.py)
LAKEBASE_ENDPOINT = os.environ.get("LAKEBASE_ENDPOINT") or (
    f"projects/{_req('BEV_LAKEBASE_PROJECT')}/branches/{os.environ.get('BEV_LAKEBASE_BRANCH', 'production')}"
    f"/endpoints/{os.environ.get('BEV_LAKEBASE_ENDPOINT', 'primary')}")
PGDATABASE = os.environ.get("BEV_LAKEBASE_DATABASE", "databricks_postgres")

PLANNER_GROUP = _req("BEV_PLANNER_GROUP") if IN_APPS else os.environ.get("BEV_PLANNER_GROUP", "bev_ops_planner_local")
APPROVER_GROUP = _req("BEV_APPROVER_GROUP") if IN_APPS else os.environ.get("BEV_APPROVER_GROUP", "bev_ops_approver_local")
ROLES = {PLANNER_GROUP: {"label": os.environ.get("BEV_PLANNER_LABEL", "Supply planner"), "can_propose": True, "can_approve": False},
         APPROVER_GROUP: {"label": os.environ.get("BEV_APPROVER_LABEL", "Approver"), "can_propose": True, "can_approve": True}}


def self_approval_effective(requested: bool, in_apps: bool, single_presenter: bool) -> bool:
    # four-eyes by default. Inside Databricks Apps a single flag is not enough: the deployer must also opt in to
    # single-presenter mode, so a shared deployment never loses four-eyes by one mistyped variable
    return requested and (not in_apps or single_presenter)


# four-eyes: the requester never decides their own request. true = exception for one presenter holding both roles
SELF_APPROVAL_REQUESTED = os.environ.get("BEV_ALLOW_SELF_APPROVAL", "false").lower() == "true"
SINGLE_PRESENTER_MODE = os.environ.get("BEV_SINGLE_PRESENTER_MODE", "") == "1"
ALLOW_SELF_APPROVAL = self_approval_effective(SELF_APPROVAL_REQUESTED, IN_APPS, SINGLE_PRESENTER_MODE)
SELF_APPROVAL_IGNORED = SELF_APPROVAL_REQUESTED and not ALLOW_SELF_APPROVAL
if ALLOW_SELF_APPROVAL:
    logging.getLogger(__name__).warning("self-approval is ON: requesters can decide their own requests (four-eyes off)")
elif SELF_APPROVAL_IGNORED:
    logging.getLogger(__name__).warning("BEV_ALLOW_SELF_APPROVAL=true ignored in Databricks Apps without BEV_SINGLE_PRESENTER_MODE=1")
# per-user chat turns per minute (token bucket); 0 = no limit
CHAT_RATE_PER_MIN = float(os.environ.get("BEV_CHAT_RATE_PER_MIN", "10"))
# above the threshold only members of the manager group can approve. Empty / unset = the approver group (every approver
# may approve any size). A separate group is checked through the user's own workspace groups (identity._roles).
MANAGER_GROUP = os.environ.get("BEV_MANAGER_GROUP", "").strip() or APPROVER_GROUP
if MANAGER_GROUP != APPROVER_GROUP and not _IDENT.match(MANAGER_GROUP.replace("-", "_")):
    raise RuntimeError("BEV_MANAGER_GROUP must be a plain group name")
MANAGER_THRESHOLD_CASES = float(os.environ.get("BEV_MANAGER_THRESHOLD_CASES", "500"))

_fast = os.environ.get("BEV_LLM_FAST_SERVICE", "")
LLM_FAST = _fast if _fast and not _fast.startswith("<") else f"{CATALOG}.{SCHEMA}.llm_fast"  # Gateway model service
EMBEDDING_SERVICE = _req("BEV_EMBEDDING_SERVICE")
GENIE_COMMERCIAL = os.environ.get("BEV_GENIE_COMMERCIAL", "")
GENIE_SUPPLY = os.environ.get("BEV_GENIE_SUPPLY", "")

# stub = scripted preview over the real tools; graph = the LangGraph agent in agent/
AGENT_MODE = os.environ.get("BEV_AGENT_MODE", "stub")
OOS_THRESHOLD_DAYS = float(os.environ.get("BEV_OOS_THRESHOLD_DAYS", "7"))
INSIGHTS_TTL_S = int(os.environ.get("BEV_INSIGHTS_TTL_S", "600"))
INSIGHTS_MAX_TOKENS = int(os.environ.get("BEV_INSIGHTS_MAX_TOKENS", "250"))

DEMO_CASES = Path(os.environ.get("BEV_DEMO_CASES", "config/demo_cases.yaml"))
DEMO_CASES = DEMO_CASES if DEMO_CASES.is_absolute() else ROOT / DEMO_CASES
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
