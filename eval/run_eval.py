"""Run the Beverage Ops Agent eval with mlflow.genai.evaluate, calling GraphBridge in-process.
Modes (--mode / BEV_EVAL_MODE):
  sessions (default): each record runs as the app does, one bev_ops_turn trace per turn grouped by mlflow.trace.session
           (eval-<run>-<record>-<hex>); then evaluate(data=<those traces>) with session-level scorers, no predict_fn.
  legacy: one bev_ops_eval trace per record wrapping all turns (predict_fn).
Usage:
  python eval/run_eval.py --check                      # validate dataset + scorers offline, no network
  python eval/run_eval.py [--mode sessions|legacy] [--no-judges] [--no-native] [--ids c1-01,m-02] [--cases memory,oos] [--to-uc] [--keep]
Env: repo .env (profile, Lakebase project, experiment id, UC trace schema); optional BEV_JUDGE_MODEL, BEV_EVAL_WORKERS,
BEV_EVAL_CASE_TIMEOUT_S (300), BEV_EVAL_RUN_TIMEOUT_MIN (45), BEV_EVAL_TRACE_WAIT_S (300).
Approval interrupts are resolved deterministically: the record's `approval` value, else "reject". Rows written under the
run's eval users (decisions, requests, checkpoints) are deleted at the end unless --keep.
"""
import os as _os
_os.environ.setdefault("BEV_SURFACE", "eval")  # request tag: who is calling the Gateway
import argparse
import json
import os
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
for p in (os.path.join(ROOT, "setup"), ROOT, os.path.join(ROOT, "app"), HERE):
    sys.path.insert(0, p)
os.environ.setdefault("LOCAL_DEV", "true")  # app settings: CLI identity, owner Lakebase connection
import config as C  # noqa: E402

UC_DATASET = f"{C.CATALOG}.{C.SCHEMA}.agent_eval_dataset"
ROUTES = {"genie_commercial", "genie_supply", "uc_function", "rag", "memory", "refuse", "clarify"}
RUN = os.environ.get("BEV_EVAL_RUN") or uuid.uuid4().hex[:6]
DEFAULT_RESOLUTION = os.environ.get("BEV_EVAL_DEFAULT_RESOLUTION", "reject")
CASE_TIMEOUT_S = int(os.environ.get("BEV_EVAL_CASE_TIMEOUT_S", "300"))
RUN_TIMEOUT_MIN = int(os.environ.get("BEV_EVAL_RUN_TIMEOUT_MIN", "45"))
TRACE_WAIT_S = int(os.environ.get("BEV_EVAL_TRACE_WAIT_S", "300"))
EVAL_MODE = os.environ.get("BEV_EVAL_MODE", "sessions")
# fail fast instead of MLflow's 120 s x 7 retries per call when the workspace or auth hiccups
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "60")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "3")


def load(ids=None, cases=None, delta_days=0):
    # dataset.jsonl is canonical; dates, week ids and doc-id years follow the demo clock (setup/demo_clock.py)
    from demo_clock import shift_obj
    rows = [json.loads(l) for l in open(os.path.join(HERE, "dataset.jsonl")) if l.strip()]
    return [shift_obj(r, delta_days) for r in rows if (not ids or r["id"] in ids) and (not cases or r["tags"]["case"] in cases)]


def live_delta():
    from demo_clock import current_delta_days
    from sqlrun import client, run
    return current_delta_days(run, client())


def check(rows):
    from scorers import TOOL_PATTERNS, get_scorers, get_session_scorers
    for r in rows:
        e, i = r["expectations"], r["inputs"]
        assert e["expected_route"] in ROUTES and set(e["acceptable_routes"]) <= ROUTES, r["id"]
        assert set(e["expected_tools"]) | set(e["forbidden_tools"]) <= set(TOOL_PATTERNS), r["id"]
        assert e["expected_facts"] and i["messages"][-1]["role"] == "user", r["id"]
        for d in i.get("setup_decisions") or []:
            assert d["action"] in ("approved", "rejected") and d["reason"], r["id"]
        for q in i.get("setup_requests") or []:
            assert q["status"] in ("proposed", "approved", "rejected", "withdrawn") and q["qty_cases"] > 0, r["id"]
    sess = get_session_scorers({}, include_judges=False)
    assert all(x.is_session_level_scorer for x in sess)
    print(f"dataset OK: {len(rows)} records; deterministic scorers: {[s.name for s in get_scorers(include_judges=False)]}; "
          f"session wrappers: {len(sess)}")


def eval_email(user_id):
    # run-scoped identity so memory never leaks between runs and cleanup is one LIKE
    local, domain = user_id.split("@", 1)
    return f"{local}+eval{RUN}@{domain}"


class Harness:
    def __init__(self):
        from agent.bridge import GraphBridge
        from server import settings as S
        self.S = S
        self.bridge = GraphBridge()

    def user(self, email):
        from server.identity import User
        return User(email=email, token=None, roles=[self.S.PLANNER_GROUP, self.S.APPROVER_GROUP])

    def turn(self, thread, user, payload, kind, inputs):
        # sync event stream on the eval's thread: GraphBridge.run hops to an executor without copying contextvars, so its spans would leave the eval trace
        ev = {"answer": [], "approval": None, "decision": None, "error": None, "trace_id": None}
        for e in self.bridge.events(thread, user, payload, kind, inputs):
            t = e.get("type")
            if t == "_trace":
                ev["trace_id"] = e["trace_id"]
            elif t == "token":
                ev["answer"].append(e["text"])
            elif t == "tool_start":
                ev["answer"] = []  # text before a tool call is a preamble; score the final reply
            elif t == "approval_required":
                ev["approval"] = e["request"]
            elif t == "decision":
                ev["decision"] = {"status": e.get("status"), "request_id": (e.get("request") or {}).get("request_id")}
            elif t == "error":
                ev["error"] = e.get("message")
        ev["answer"] = "".join(ev["answer"])
        return ev

    def conversation(self, user, messages, resolution, thread):
        from langchain_core.messages import HumanMessage
        from langgraph.types import Command
        turns = []
        for m in messages:
            if m["role"] != "user":
                continue  # the agent writes its own replies; recorded assistant turns are context for humans only
            t = self.turn(thread, user, {"messages": [HumanMessage(m["content"])]}, "message", {"message": m["content"]})
            turns.append(t)
            while t["approval"]:  # never leave a pending request
                payload = Command(resume={"decision": resolution, "reason": f"eval auto-{resolution}"})
                t = self.turn(thread, user, payload, "resume", {"decision": resolution})
                turns.append(t)
        return turns


def seed_decisions(user_email, decisions):
    from lakebase_conn import connect
    from memory.decisions import save_decision, summarize
    from rag.search import embed
    from sqlrun import client
    w = client()
    emb = lambda texts: embed(texts, w.config.host, w.config.authenticate(), C.EMBEDDING_SERVICE)
    with connect() as conn:
        for d in decisions:
            ref = conn.execute("""SELECT d.distributor_name, d.market_id, p.product_name FROM agent_ops.ref_distributor d, agent_ops.ref_product p
                                  WHERE d.distributor_id = %s AND p.sku = %s""", (d["distributor_id"], d["sku"])).fetchone()
            summary = summarize(d["action"], d.get("qty_cases"), ref[2], d["sku"], d["distributor_id"], ref[0], ref[1], d["reason"])
            row = save_decision(conn, user_email, d["action"], d["reason"], summary, market_id=ref[1], distributor_id=d["distributor_id"],
                                sku=d["sku"], qty_cases=d.get("qty_cases"), embed_fn=emb)
            conn.execute("UPDATE agent_mem.decision_log SET created_at = now() - make_interval(days => %s) WHERE decision_id = %s",
                         (int(d.get("days_ago", 1)), row["decision_id"]))
        conn.commit()


def seed_requests(user_email, requests):
    # ledger rows for summary cases: proposed, then moved to the record's final status
    from lakebase_conn import connect
    from memory.replenishment import propose_request
    with connect() as conn:
        for r in requests:
            row = propose_request(conn, user_email, r["distributor_id"], r["sku"], r["qty_cases"], r.get("reason") or "eval seed")
            if r["status"] != "proposed":
                conn.execute("""UPDATE agent_ops.replenishment_requests SET status = %s, decided_by = %s, decided_at = now()
                                WHERE request_id = %s""", (r["status"], user_email, row["request_id"]))
        conn.commit()


def cleanup():
    from lakebase_conn import connect
    pat = f"%+eval{RUN}@%"
    state = os.environ.get("BEV_AGENT_STATE_SCHEMA", "agent_state")
    with connect() as conn:
        n = [conn.execute("DELETE FROM agent_mem.decision_log WHERE user_id LIKE %s", (pat,)).rowcount,
             conn.execute("DELETE FROM agent_ops.replenishment_requests WHERE requested_by LIKE %s OR decided_by LIKE %s", (pat, pat)).rowcount]
        for t in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            n.append(conn.execute(f"DELETE FROM {state}.{t} WHERE thread_id LIKE %s", (f"eval-{RUN}-%",)).rowcount)
        conn.commit()
    print(f"cleanup run {RUN}: decisions {n[0]}, requests {n[1]}, checkpoint rows {sum(n[2:])}")


def _with_deadline(fn, timeout_s):
    # run in a daemon thread with the caller's contextvars (keeps spans in the eval trace); a stuck case returns instead of blocking
    import contextvars
    import threading
    box, ctx = {}, contextvars.copy_context()

    def target():
        try:
            box["out"] = ctx.run(fn)
        except Exception as ex:  # noqa: BLE001
            box["err"] = ex

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"case exceeded {timeout_s}s")
    if "err" in box:
        raise box["err"]
    return box["out"]


def run_case(h, rid, messages, user_id, approval, setup_turns, setup_decisions, setup_requests=None):
    # one dataset record: seed memory, earlier sessions ("day 1"), then the scored conversation in its own thread
    email = eval_email(user_id)
    user = h.user(email)
    if setup_decisions:
        seed_decisions(email, setup_decisions)
    if setup_requests:
        seed_requests(email, setup_requests)
    for n, st in enumerate(setup_turns or []):
        h.conversation(user, st["messages"], st.get("approval", DEFAULT_RESOLUTION), f"eval-{RUN}-{rid}~setup{n}-{st['session']}-{uuid.uuid4().hex[:6]}")
    thread = f"eval-{RUN}-{rid}-{uuid.uuid4().hex[:8]}"
    turns = h.conversation(user, messages, approval or DEFAULT_RESOLUTION, thread)
    last = next((t for t in reversed(turns) if t["answer"]), turns[-1] if turns else {"answer": ""})
    return thread, turns, {"response": last["answer"], "decisions": [t["decision"] for t in turns if t["decision"]],
                           "approvals_requested": sum(1 for t in turns if t["approval"]), "errors": [t["error"] for t in turns if t["error"]]}


def make_predict_fn(h):
    import mlflow

    @mlflow.trace(name="bev_ops_eval", span_type="AGENT")
    def predict_fn(messages, user_id, approval=None, setup_turns=None, setup_decisions=None, setup_requests=None):
        try:
            return _with_deadline(lambda: run_case(h, "legacy", messages, user_id, approval, setup_turns, setup_decisions, setup_requests)[2], CASE_TIMEOUT_S)
        except TimeoutError as ex:
            return {"response": "", "decisions": [], "approvals_requested": 0, "errors": [str(ex)]}

    return predict_fn


def run_sessions(h, rows, workers):
    # run phase: records in parallel, each turn traced like the app (no wrapper span)
    from concurrent.futures import ThreadPoolExecutor
    def one(r):
        i = r["inputs"]
        try:
            thread, turns, out = _with_deadline(lambda: run_case(h, r["id"], i["messages"], i["user_id"], i.get("approval"),
                                                                 i.get("setup_turns"), i.get("setup_decisions"),
                                                                 i.get("setup_requests")), CASE_TIMEOUT_S)
        except Exception as ex:  # noqa: BLE001  timeout or failure: the record is scored on whatever traces exist
            print(f"  {r['id']}: {type(ex).__name__}: {str(ex)[:200]}")
            return r, None, [], {"response": "", "decisions": [], "approvals_requested": 0, "errors": [str(ex)]}
        print(f"  {r['id']}: {len(turns)} turn(s)")
        return r, thread, [t["trace_id"] for t in turns if t["trace_id"]], out
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, rows))


def collect_traces(sessions, expected):
    # UC trace export is async: poll until every turn trace of the scored sessions is readable
    import time
    import mlflow
    exp = os.environ["BEV_MLFLOW_EXPERIMENT_ID"]
    t0, got = time.time(), []
    while True:
        got = mlflow.search_traces(locations=[exp], filter_string=f"metadata.`mlflow.trace.session` LIKE 'eval-{RUN}-%'",
                                   return_type="list", max_results=5000)
        got = [t for t in got if (t.info.trace_metadata or {}).get("mlflow.trace.session") in sessions]
        if len(got) >= expected or time.time() - t0 > TRACE_WAIT_S:
            break
        print(f"  waiting for traces: {len(got)}/{expected}")
        time.sleep(10)
    if len(got) < expected:
        print(f"WARNING: only {len(got)}/{expected} turn traces readable after {TRACE_WAIT_S}s")
    return got


def evaluate_sessions(h, rows, a, max_words):
    import mlflow
    from scorers import get_session_scorers
    results = run_sessions(h, rows, int(os.environ.get("BEV_EVAL_WORKERS", "4")))
    mlflow.flush_trace_async_logging()
    records = {th: {"id": r["id"], "inputs": r["inputs"], "outputs": out, "expectations": r["expectations"]}
               for r, th, _, out in results if th}
    expected = sum(len(ids) for _, _, ids, _ in results)
    traces = collect_traces(set(records), expected)
    data, seen = [], set()
    for t in sorted(traces, key=lambda t: t.info.request_time or 0):
        rec = records[t.info.trace_metadata["mlflow.trace.session"]]
        first = rec["id"] not in seen
        seen.add(rec["id"])
        # expectations ride on the session's first trace, where session assessments are logged
        data.append({"trace": t, "expectations": rec["expectations"] if first else {},
                     "tags": {"record_id": rec["id"], "eval_run": RUN}})
    print(f"score phase: {len(data)} turn traces in {len(seen)} sessions")
    scorers = get_session_scorers(records, include_judges=not a.no_judges, max_words=max_words, native=not a.no_native)
    return mlflow.genai.evaluate(data=data, scorers=scorers)


def _watchdog(minutes, keep):
    # whole-run guard: dump Python stacks, clean Lakebase rows, exit non-zero
    import faulthandler
    import threading

    def fire():
        print(f"\nWATCHDOG: run {RUN} exceeded {minutes} min; stacks follow", file=sys.stderr, flush=True)
        faulthandler.dump_traceback(all_threads=True)
        if not keep:
            try:
                cleanup()
            except Exception as ex:  # noqa: BLE001
                print("watchdog cleanup failed:", ex, file=sys.stderr)
        os._exit(2)

    t = threading.Timer(minutes * 60, fire)
    t.daemon = True
    t.start()
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--no-judges", action="store_true")
    ap.add_argument("--no-native", action="store_true", help="sessions mode: skip MLflow's conversation judges")
    ap.add_argument("--mode", choices=["sessions", "legacy"], default=EVAL_MODE)
    ap.add_argument("--ids", default="")
    ap.add_argument("--cases", default="")
    ap.add_argument("--keep", action="store_true", help="keep eval rows in Lakebase (debugging)")
    ap.add_argument("--to-uc", action="store_true", help="merge records into the UC eval dataset")
    a = ap.parse_args()
    ids, cases = (set(a.ids.split(",")) if a.ids else None), (set(a.cases.split(",")) if a.cases else None)
    if a.check:
        return check(load(ids, cases))  # offline: canonical dates
    delta = live_delta()
    rows = load(ids, cases, delta)
    os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", os.environ.get("BEV_EVAL_WORKERS", "4"))
    os.environ.setdefault("MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION", "true")  # the validation call would run the first record twice
    import mlflow
    from scorers import get_scorers
    h = Harness()  # init_tracing: experiment BEV_MLFLOW_EXPERIMENT_ID + UC trace location
    sys.path.insert(0, os.path.join(ROOT, "mlflow_setup"))
    import params as P
    mlflow.set_registry_uri(f"databricks-uc://{C.PROFILE}")
    prompts = {n: mlflow.genai.load_prompt(f"prompts:/{n}@{P.ALIAS}") for n in P.FILES}
    pparams = P.prompt_params()
    data = [{"inputs": r["inputs"], "expectations": r["expectations"], "tags": r["tags"] | {"record_id": r["id"]}} for r in rows]
    if a.to_uc and a.mode == "legacy":
        # MLflow-managed dataset needs a Spark session (databricks-connect serverless)
        import mlflow.genai.datasets
        ds = mlflow.genai.datasets.create_dataset(uc_table_name=UC_DATASET)
        ds.merge_records(data)
        data = ds
    print(f"eval run {RUN} ({a.mode}): {len(rows)} records, demo-clock delta {delta} days, case timeout {CASE_TIMEOUT_S}s, run timeout {RUN_TIMEOUT_MIN} min")
    dog = _watchdog(RUN_TIMEOUT_MIN, a.keep)
    try:
        if a.mode == "sessions":
            res = evaluate_sessions(h, rows, a, int(pparams["max_words"]))
        else:
            res = mlflow.genai.evaluate(data=data, predict_fn=make_predict_fn(h),
                                        scorers=get_scorers(include_judges=not a.no_judges, max_words=int(pparams["max_words"])))
    finally:
        mlflow.flush_trace_async_logging()
        h.bridge.close()
        if not a.keep:
            cleanup()
        dog.cancel()
    with mlflow.start_run(run_id=res.run_id):
        mlflow.set_tags({f"prompt.{n.split('.')[-1]}": f"{n}/{p.version}@{P.ALIAS}" for n, p in prompts.items()} | {"eval_run": RUN, "eval_mode": a.mode, "demo_clock_delta_days": str(delta)})
    print(json.dumps(res.metrics, indent=1, default=str))
    print("run_id", res.run_id)


if __name__ == "__main__":
    main()
