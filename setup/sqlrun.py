"""Run a parameterized SQL file (${catalog}, ${schema}, ${volume}) on the configured warehouse."""
import re
import sys
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

import config


def client():
    return WorkspaceClient(profile=config.PROFILE)


def run(sql, w=None):
    w = w or client()
    r = w.statement_execution.execute_statement(warehouse_id=config.WAREHOUSE_ID, statement=sql, wait_timeout="50s")
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"{r.status.state}: {r.status.error.message if r.status.error else ''}\n{sql[:400]}")
    return r.result.data_array if r.result else None


def render(path, extra=None):
    p = config.params() | (extra or {})
    return re.sub(r"\$\{(\w+)\}", lambda m: str(p[m.group(1)]), open(path).read())


def run_file(path, extra=None):
    w = client()
    for stmt in [s.strip() for s in render(path, extra).split(";\n") if s.strip()]:
        print("->", stmt.splitlines()[0][:110])
        out = run(stmt, w)
        for row in (out or [])[:20]:
            print("  ", row)


if __name__ == "__main__":
    for f in sys.argv[1:]:
        run_file(f)
