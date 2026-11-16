"""Per-request context: the user's WorkspaceClient (OBO), identity, Lakebase connection factory by role."""
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

from databricks.sdk import WorkspaceClient

# local dev and setup scripts: targets/<BEV_TARGET>.env + .resolved.env (the app gets the same keys from app.yaml env)
_T = os.environ.get("BEV_TARGET", "")
for _ENV in ([os.path.join(os.path.dirname(__file__), "..", "targets", f"{_T}{x}") for x in (".env", ".resolved.env")] if _T else []):
    if not os.path.exists(_ENV):
        continue
    for _l in open(_ENV):
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

CATALOG = os.environ.get("BEV_CATALOG", "")
# same default as setup/config.py (bev_ops_<env>) so local runs match the deployed schema
SCHEMA = os.environ.get("BEV_SCHEMA") or (f"bev_ops_{os.environ['BEV_ENV']}" if os.environ.get("BEV_ENV") else "")
LLM_FAST = os.environ.get("BEV_LLM_FAST_SERVICE", f"{CATALOG}.{SCHEMA}.llm_fast")
LLM_REASONING = os.environ.get("BEV_LLM_REASONING_SERVICE", f"{CATALOG}.{SCHEMA}.llm_reasoning")
EMBEDDING_SERVICE = os.environ.get("BEV_EMBEDDING_SERVICE", "system.ai.gte-large-en")
GENIE_COMMERCIAL = os.environ.get("BEV_GENIE_COMMERCIAL", "")
GENIE_SUPPLY = os.environ.get("BEV_GENIE_SUPPLY", "")
DBSQL_MCP = os.environ.get("BEV_DBSQL_MCP", "/ai-gateway/mcp-services/system.ai.dbsql")


@dataclass
class RequestContext:
    wc: WorkspaceClient
    user_email: str
    # pg(role) -> context manager yielding a read connection for "planner" | "approver" (group roles are read-only)
    pg: Callable = field(repr=False)
    allow_self_approval: bool = False
    roles: list = field(default_factory=list)
    can_propose: bool = True
    manager_ok: bool = False
    # the user may decide their own proposals in chat (demo self-approval + approver role); else they go to the Requests queue
    can_decide_own: bool = False
    # service() -> context manager yielding the app SP connection used for writes (after the app checked the role)
    service: Optional[Callable] = field(default=None, repr=False)

    def write(self):
        # scripts/eval/spikes have no app SP: the local owner connection behind pg() is used instead
        return self.service() if self.service else self.pg("approver")

    def headers(self):
        return self.wc.config.authenticate()

    def embed(self, texts):
        from rag.search import embed
        return embed(texts, self.wc.config.host, self.headers(), EMBEDDING_SERVICE)


def local_context(profile=None):
    # local dev: CLI profile identity, owner connection for every role
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))
    import config as C
    from lakebase_conn import connect

    wc = WorkspaceClient(profile=profile or C.PROFILE)

    @contextmanager
    def pg(role):
        with connect() as conn:
            yield conn

    # local dev: one identity plays every role (documented demo-only exception)
    return RequestContext(wc=wc, user_email=wc.current_user.me().user_name, pg=pg, allow_self_approval=True, manager_ok=True,
                          can_decide_own=True)
