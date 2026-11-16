"""Demo prep: wake every backend the agent touches and print how long each took. Exit 1 if any component fails.
Components: Lakebase endpoint, SQL warehouse, both Genie spaces, Gateway model services (fast, reasoning) and embeddings.
Usage: python3 scripts/warmup.py [--only lakebase,warehouse,genie,llm,embed] [--ask-genie] [--warehouse-timeout 600]
  --ask-genie  ask each Genie space one real question (BEV_WARMUP_GENIE_QUESTION, ~15-40 s each); default only checks the space.
"""
import argparse
import os
import sys
import time
from datetime import timedelta

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "setup"))
sys.path.insert(0, ROOT)
import config as C  # noqa: E402

from agent import context as A  # noqa: E402

COMPONENTS = ["lakebase", "warehouse", "genie", "llm", "embed"]
GENIE_QUESTION = os.environ.get("BEV_WARMUP_GENIE_QUESTION", "How many markets are there?")


def lakebase(wc):
    from lakebase_conn import connect
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()
    return "SELECT 1 ok"


def warehouse(wc, timeout_s):
    from databricks.sdk.service.sql import State
    wid = C.WAREHOUSE_ID
    if not wid:
        raise RuntimeError("BEV_WAREHOUSE_ID is not set")
    st = wc.warehouses.get(wid).state
    if st != State.RUNNING:
        wc.warehouses.start(wid)
        wc.warehouses.wait_get_warehouse_running(wid, timeout=timedelta(seconds=timeout_s))
    r = wc.statement_execution.execute_statement(statement="SELECT 1", warehouse_id=wid, wait_timeout="30s")
    return f"was {st.value if st else '?'}, SELECT 1 {r.status.state.value}"


def genie(wc, ask):
    out = []
    for label, sid in (("commercial", A.GENIE_COMMERCIAL), ("supply", A.GENIE_SUPPLY)):
        if ask:
            from agent.mcp_client import genie_ask
            d = genie_ask(A.local_context(), sid, GENIE_QUESTION)
            if d.get("status") != "COMPLETED":
                raise RuntimeError(f"{label}: {d.get('error') or d.get('status')}")
        else:
            wc.genie.get_space(sid)
        out.append(label)
    return ("asked " if ask else "space ok: ") + ", ".join(out)


def llm(wc):
    from databricks_langchain import ChatDatabricks
    out = []
    for svc in (A.LLM_FAST, A.LLM_REASONING):
        t = time.time()
        ChatDatabricks(model=svc, use_ai_gateway=True, workspace_client=wc, max_tokens=16).invoke("Reply with OK.")
        out.append(f"{svc.rsplit('.', 1)[-1]} {time.time() - t:.1f}s")
    return ", ".join(out)


def embed(wc):
    from rag.search import embed as _embed
    v = _embed(["warm-up"], wc.config.host, wc.config.authenticate(), A.EMBEDDING_SERVICE)[0]
    return f"{A.EMBEDDING_SERVICE} dim {len(v)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=",".join(COMPONENTS))
    ap.add_argument("--ask-genie", action="store_true")
    ap.add_argument("--warehouse-timeout", type=int, default=int(os.environ.get("BEV_WARMUP_WAREHOUSE_TIMEOUT_S", "600")))
    a = ap.parse_args()
    from databricks.sdk import WorkspaceClient
    wc = WorkspaceClient(profile=C.PROFILE)
    steps = {"lakebase": lambda: lakebase(wc), "warehouse": lambda: warehouse(wc, a.warehouse_timeout),
             "genie": lambda: genie(wc, a.ask_genie), "llm": lambda: llm(wc), "embed": lambda: embed(wc)}
    failed = []
    for name in [s.strip() for s in a.only.split(",") if s.strip()]:
        t = time.time()
        try:
            detail = steps[name]()
            print(f"OK    {name:<10} {time.time() - t:6.1f}s  {detail}", flush=True)
        except Exception as e:
            failed.append(name)
            print(f"FAIL  {name:<10} {time.time() - t:6.1f}s  {type(e).__name__}: {str(e)[:200]}", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
