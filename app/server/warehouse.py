"""Parameterized UC function calls on the SQL warehouse, as the user (OBO token; CLI profile in LOCAL_DEV)."""
import time

from databricks.sdk.service.sql import StatementParameterListItem, StatementState

from server import settings as S

_NUM = {"INT", "LONG", "BIGINT", "SHORT", "BYTE", "DOUBLE", "FLOAT", "DECIMAL"}


def query(w, sql: str, params: dict | None = None, timeout_s: int = 60) -> list[dict]:
    if not S.WAREHOUSE_ID:
        raise RuntimeError("BEV_WAREHOUSE_ID is not set")
    plist = [StatementParameterListItem(name=k, value=None if v is None else str(v), type=_ptype(v))
             for k, v in (params or {}).items()]
    r = w.statement_execution.execute_statement(warehouse_id=S.WAREHOUSE_ID, statement=sql, parameters=plist,
                                                wait_timeout="30s")
    deadline = time.time() + timeout_s
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        if time.time() > deadline:
            w.statement_execution.cancel_execution(r.statement_id)
            raise TimeoutError("warehouse query timed out")
        time.sleep(1)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(r.status.error.message if r.status.error else str(r.status.state))
    cols = [(c.name, (c.type_name.value if c.type_name else "STRING")) for c in r.manifest.schema.columns]
    rows = (r.result.data_array if r.result else None) or []
    return [{n: _cast(v, t) for (n, t), v in zip(cols, row)} for row in rows]


def _ptype(v):
    if isinstance(v, bool):
        return "BOOLEAN"
    if isinstance(v, int):
        return "INT"
    if isinstance(v, float):
        return "DOUBLE"
    return "STRING"


def _cast(v, t):
    if v is None:
        return None
    if t in _NUM:
        return float(v)
    if t == "BOOLEAN":
        return v.lower() == "true"
    return v


def fn(name: str) -> str:
    # catalog/schema validated as identifiers in settings
    return f"{S.CATALOG}.{S.SCHEMA}.{name}"
