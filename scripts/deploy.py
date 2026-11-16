#!/usr/bin/env python3
"""Deploy the Beverage Ops Agent app from a laptop. Idempotent steps.

Examples
  BEV_TARGET=<target> python scripts/deploy.py all --dry-run
  BEV_TARGET=<target> python scripts/deploy.py all
  python scripts/deploy.py build                                  # frontend + stage shared packages only
  BEV_TARGET=<target> python scripts/deploy.py verify

Prerequisites (run once, as the Lakebase project owner): setup/40-41 (RAG), 50-51 (memory), 53 (agent_ui), then 52 --apply (group roles).
MLflow: mlflow_setup/01_experiment.py (experiment + UC trace tables) and 02_prompts.py; pass mlflow_experiment_id as a bundle var.
Tracing access is declared as app resources (resources/app.yml); the `grants` step (before the app starts) adds the Prompt Registry grant on the prompt schema.
"""
import argparse
import os
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
FRONTEND = APP / "frontend"
# packages the app imports, staged into app/ (gitignored copies); mlflow_setup carries prompt_params.yaml + fallback templates
SHARED = ["config", "memory", "rag", "agent", "mlflow_setup"]
# grants before app: deploy already created the app and its SP, so the app starts with its prompt/UC/Lakebase grants
# in place (otherwise the first prompt load fails until the 60 s retry); verify re-checks the Lakebase no-CREATE after start
STEPS = ["build", "validate", "deploy", "grants", "app", "verify"]
REQUIRED_SCOPES = {"sql", "postgres", "genie", "ai-gateway"}
DRY = False
sys.path.insert(0, str(ROOT / "setup"))
import config as C  # noqa: E402  (loads targets/<BEV_TARGET>.env + .resolved.env)

# bundle variables, all from the target (setup/config.py); deploy-only knobs keep safe defaults
DEPLOY_DEFAULTS = {"BEV_DEPLOY_AGENT_MODE": "graph", "BEV_DEPLOY_PROMPT_ALIAS": "production",
                   "BEV_DEPLOY_ALLOW_SELF_APPROVAL": "false", "BEV_DEPLOY_SINGLE_PRESENTER_MODE": "0"}
for _k, _v in DEPLOY_DEFAULTS.items():
    os.environ.setdefault(_k, _v)


def target_vars():
    return {"env": C.ENV, "catalog": C.CATALOG, "schema": C.SCHEMA, "warehouse_id": C.WAREHOUSE_ID,
            "app_name": C.APP_NAME, "lakebase_project_id": C.LAKEBASE_PROJECT,
            "planner_group": C.PLANNER_GROUP, "approver_group": C.APPROVER_GROUP, "viewer_group": C.VIEWER_GROUP,
            "manager_group": C.MANAGER_GROUP or C.APPROVER_GROUP, "prompt_schema": C.PROMPT_SCHEMA, "trace_salt_scope": C.SECRET_SCOPE,
            "genie_commercial": C.GENIE_COMMERCIAL, "genie_supply": C.GENIE_SUPPLY,
            "mlflow_experiment_id": C.MLFLOW_EXPERIMENT_ID, "embedding_service": C.EMBEDDING_SERVICE,
            "tag_project": C.TAGS.get("bev_project", f"bev_ops_{C.ENV}"), "tag_owner": C.TAGS.get("owner", "unset"),
            "agent_mode": os.environ["BEV_DEPLOY_AGENT_MODE"], "prompt_alias": os.environ["BEV_DEPLOY_PROMPT_ALIAS"],
            "allow_self_approval": os.environ["BEV_DEPLOY_ALLOW_SELF_APPROVAL"],
            "single_presenter_mode": os.environ["BEV_DEPLOY_SINGLE_PRESENTER_MODE"]}


def log(msg):
    print(f"[deploy] {msg}", flush=True)


def run(cmd, *, capture=False, check=True, cwd=ROOT, mutate=True, env=None):
    shown = " ".join(cmd)
    if DRY and mutate:
        log(f"DRY-RUN  {shown}")
        return ""
    log(shown)
    exe = shutil.which(cmd[0]) or cmd[0]  # npm.cmd / databricks.exe on Windows
    res = subprocess.run([exe, *cmd[1:]], cwd=cwd, text=True, capture_output=capture,
                         env={**os.environ, **env} if env else None)
    if check and res.returncode != 0:
        if capture:
            sys.stderr.write(res.stdout + res.stderr)
        raise SystemExit(f"[deploy] failed ({res.returncode}): {shown}")
    return res.stdout if capture else ""


def bundle(args, ctx, **kw):
    extra = [x for k, v in ctx.vars.items() for x in ("--var", f"{k}={v}")]
    return run(["databricks", "bundle", *args, "-t", ctx.target, "-p", ctx.profile, *extra], **kw)


def build(ctx):
    run(["npm", "ci"], cwd=FRONTEND)
    run(["npm", "run", "build"], cwd=FRONTEND)
    for name in SHARED:
        src, dst = ROOT / name, APP / name
        if not src.exists():
            log(f"skip {name}/ (not present)")
            continue
        if DRY:
            log(f"DRY-RUN  stage {name}/ -> app/{name}/")
            continue
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "out"))
        log(f"staged {name}/ -> app/{name}/")


def clean():
    # staged copies shadow the repo packages for local runs (app/ is on sys.path): drop them once the bundle is uploaded
    for name in SHARED:
        dst = APP / name
        if dst.exists() and not DRY:
            shutil.rmtree(dst)
            log(f"removed staged app/{name}/")


def validate(ctx):
    bundle(["validate"], ctx, mutate=False)


def deploy(ctx):
    bundle(["deploy"], ctx)


def app(ctx):
    # deploy + (re)start: a bundle deploy alone does not restart a running app
    bundle(["run", "bev_ops_agent"], ctx)


def resolved_vars(ctx):
    # bundle variables after target + --var resolution
    out = bundle(["validate", "-o", "json"], ctx, capture=True, mutate=False)
    return {k: (v or {}).get("value") for k, v in (json.loads(out[out.index("{"):]).get("variables") or {}).items()}


def _sql(ctx, wh, sql, missing_principal_ok=False):
    body = json.dumps({"warehouse_id": wh, "statement": sql, "wait_timeout": "30s"})
    res = json.loads(run(["databricks", "api", "post", "/api/2.0/sql/statements", "--json", body, "-p", ctx.profile], capture=True,
                         mutate=not sql.lstrip().upper().startswith("SELECT")))
    state = (res.get("status") or {}).get("state")
    if state != "SUCCEEDED" and missing_principal_ok and "PRINCIPAL_DOES_NOT_EXIST" in json.dumps(res.get("status") or {}):
        return None
    if state != "SUCCEEDED":
        raise SystemExit(f"[deploy] statement failed ({state}): {sql}: {(res.get('status') or {}).get('error')}")
    return (res.get("result") or {}).get("data_array") or []


TRACE_TABLES = ("annotations", "logs", "metrics", "spans")
WRITE_OR_READ = {"SELECT", "MODIFY", "ALL PRIVILEGES", "ALL", "MANAGE"}
BROAD = {"account users", "users", "all users"}


def trace_prefix(v):
    prefix = v.get("trace_table_prefix") or "bev_ops"
    if not re.fullmatch(r"[a-z0-9_]+", prefix):
        raise SystemExit(f"[deploy] trace_table_prefix {prefix!r} must match [a-z0-9_]+")
    return prefix


def trace_grant_violations(rows, allowed):
    """rows: SHOW GRANTS dicts (Principal, ActionType, ObjectType, ObjectKey) for a trace table, its schema and catalog.
    Returns readable violations: a table read/write by a principal outside `allowed`, or a broad group holding
    SELECT/ALL at schema or catalog level (inherited by every table, traces included). Pure: unit-tested."""
    allowed = {a.lower() for a in allowed if a}
    out = []
    for r in rows:
        who = str(r.get("Principal") or r.get("principal") or "").strip("`").lower()
        act = str(r.get("ActionType") or r.get("action_type") or "").upper()
        kind = str(r.get("ObjectType") or r.get("object_type") or "").upper()
        key = r.get("ObjectKey") or r.get("object_key") or ""
        if kind == "TABLE" and act in WRITE_OR_READ and who not in allowed:
            out.append(f"{who} has {act} on {key}")
        elif kind in ("SCHEMA", "CATALOG") and act in {"SELECT", "ALL PRIVILEGES", "ALL"} and (who in BROAD or who not in allowed):
            out.append(f"{who} has {act} on {kind.lower()} {key} (inherited by the trace tables)")
    return sorted(set(out))


def check_trace_grants(ctx, v, sp):
    # traces hold questions, masked user ids and the rows tools returned: verify nobody else can read them,
    # including through schema/catalog grants that a table-level REVOKE cannot remove.
    # (Option, not implemented: put the trace tables in their own schema to make this structural.)
    cat, sch, wh, prefix = v["catalog"], v["schema"], v["warehouse_id"], trace_prefix(v)
    me = json.loads(run(["databricks", "current-user", "me", "-p", ctx.profile, "-o", "json"], capture=True, mutate=False))
    allowed = {v.get("viewer_group") or os.environ.get("BEV_VIEWER_GROUP"), sp, me.get("userName")}
    # known readers you accept (e.g. a workspace admin with catalog ALL PRIVILEGES); logged on every verify
    accepted = {x.strip() for x in os.environ.get("BEV_VERIFY_ACCEPTED_TRACE_READERS", "").split(",") if x.strip()}
    if accepted:
        log(f"accepted trace readers (BEV_VERIFY_ACCEPTED_TRACE_READERS): {sorted(accepted)}")
    allowed |= accepted
    rows = []
    cols = ["Principal", "ActionType", "ObjectType", "ObjectKey"]
    targets = [f"TABLE `{cat}`.`{sch}`.`{prefix}_otel_{t}`" for t in TRACE_TABLES] + [f"SCHEMA `{cat}`.`{sch}`", f"CATALOG `{cat}`"]
    for obj in targets:
        for r in _sql(ctx, wh, f"SHOW GRANTS ON {obj}") or []:
            rows.append(dict(zip(cols, r)))
    bad = trace_grant_violations(rows, allowed)
    if bad:
        raise SystemExit("[deploy] FATAL: trace tables readable beyond the viewer group / app SP / deployer:\n  " + "\n  ".join(bad))
    log(f"trace grants ok: only {sorted(a for a in allowed if a)} on {cat}.{sch}.{prefix}_otel_*")


def group_grants(ctx, v):
    # end users (OBO) read business objects and run the UC functions; trace tables (<prefix>_otel_*) stay out of reach
    # planner/approver read every business table/view except the trace tables. MLflow writes only the four
    # <prefix>_otel_* tables; verify (check_trace_grants) fails if anything else exposes them.
    cat, sch, wh, prefix = v["catalog"], v["schema"], v["warehouse_id"], trace_prefix(v)
    rows = _sql(ctx, wh, f"""SELECT table_name FROM `{cat}`.information_schema.tables
                             WHERE table_schema = '{sch}' AND table_name NOT LIKE '{prefix}\\_otel\\_%' ORDER BY 1""")
    out = []
    for g in (v["planner_group"], v["approver_group"]):
        out += [f"GRANT USE CATALOG ON CATALOG `{cat}` TO `{g}`", f"GRANT USE SCHEMA, EXECUTE ON SCHEMA `{cat}`.`{sch}` TO `{g}`"]
        out += [f"GRANT SELECT ON TABLE `{cat}`.`{sch}`.`{r[0]}` TO `{g}`" for r in rows]
    # traces hold questions, (masked) user ids and the UC rows tools returned: only the viewer group reads them
    viewer = v.get("viewer_group") or os.environ.get("BEV_VIEWER_GROUP")
    if viewer:
        out += [f"GRANT USE CATALOG ON CATALOG `{cat}` TO `{viewer}`", f"GRANT USE SCHEMA ON SCHEMA `{cat}`.`{sch}` TO `{viewer}`"]
        out += [f"GRANT SELECT ON TABLE `{cat}`.`{sch}`.`{prefix}_otel_{t}` TO `{viewer}`"
                for t in TRACE_TABLES]
    for g in (v["planner_group"], v["approver_group"]):
        out += [f"REVOKE ALL PRIVILEGES ON TABLE `{cat}`.`{sch}`.`{prefix}_otel_{t}` FROM `{g}`"
                for t in TRACE_TABLES]
    return out


def genie_grants(ctx, v):
    # both Genie Agents: CAN_RUN for the end-user groups (the agent calls them with the user's token)
    for space in (v.get("genie_commercial"), v.get("genie_supply")):
        if not space:
            continue
        acl = [{"group_name": g, "permission_level": "CAN_RUN"} for g in (v["planner_group"], v["approver_group"])]
        body = json.dumps({"access_control_list": acl})
        if DRY:
            log(f"DRY-RUN  PATCH /api/2.0/permissions/genie/{space} {body}")
            continue
        run(["databricks", "api", "patch", f"/api/2.0/permissions/genie/{space}", "--json", body, "-p", ctx.profile], capture=True)
        log(f"ok  genie {space}: CAN_RUN for {[a['group_name'] for a in acl]}")


def grants(ctx):
    # Prompt Registry: prompts are not individually grantable (no PROMPT securable) -> schema-level grant to the app SP.
    # Loading needs the documented set (EXECUTE + CREATE FUNCTION + MANAGE; EXECUTE alone is PERMISSION_DENIED, seen live).
    # MANAGE on the data schema would let the SP grant itself MODIFY on any table, so prompts live in their own
    # schema (prompt_schema, no tables) and the full set is granted there only; the data schema keeps EXECUTE.
    v = resolved_vars(ctx)
    cat, sch, wh = v["catalog"], v["schema"], v["warehouse_id"]
    psch = v.get("prompt_schema") or f"{sch}_prompts"
    if psch == sch:
        raise SystemExit("[deploy] prompt_schema must differ from schema (MANAGE would cover the data tables)")
    if DRY:
        sp = "<app-service-principal-application-id>"
    else:
        a = json.loads(run(["databricks", "apps", "get", ctx.app_name, "-p", ctx.profile, "-o", "json"], capture=True, mutate=False))
        sp = a["service_principal_client_id"]
    privs = ["EXECUTE"]
    stmts = [f"GRANT USE CATALOG ON CATALOG `{cat}` TO `{sp}`",
             f"GRANT USE SCHEMA, {', '.join(privs)} ON SCHEMA `{cat}`.`{sch}` TO `{sp}`",
             f"GRANT USE SCHEMA, EXECUTE, CREATE FUNCTION, MANAGE ON SCHEMA `{cat}`.`{psch}` TO `{sp}`"]
    for sql in stmts:
        if DRY:
            log(f"DRY-RUN  SQL on warehouse {wh}: {sql}")
            continue
        _sql(ctx, wh, sql)
        log(f"ok  {sql}")
    # end-user groups: a workspace-local group is not a UC principal -> warn and skip its UC grants (Genie + Lakebase still apply)
    skipped = set()
    for sql in group_grants(ctx, v):
        g = sql.rsplit(" TO " if " TO " in sql else " FROM ", 1)[1].strip("`")
        if g in skipped:
            continue
        if DRY:
            log(f"DRY-RUN  SQL on warehouse {wh}: {sql}")
            continue
        if _sql(ctx, wh, sql, missing_principal_ok=True) is None:
            skipped.add(g)
            log(f"WARN  {g} is not a UC principal (workspace-local group?): UC grants skipped; use an account group for other users")
            continue
        log(f"ok  {sql}")
    genie_grants(ctx, v)
    # Lakebase: groups read-only, app SP writes (role is created by the app's postgres resource)
    pg = [sys.executable, "setup/52_lakebase_access.py", "--app-sp", sp] + ([] if DRY else ["--apply"])
    # same workspace/project as this deploy, not the .env defaults
    pg_env = {"DATABRICKS_CONFIG_PROFILE": ctx.profile, "BEV_LAKEBASE_PROJECT": v.get("lakebase_project_id") or "",
              "BEV_LAKEBASE_BRANCH": v.get("lakebase_branch_id") or "", "BEV_LAKEBASE_ENDPOINT": v.get("lakebase_endpoint_id") or "",
              "BEV_LAKEBASE_DATABASE": v.get("lakebase_database") or "", "BEV_PLANNER_GROUP": v.get("planner_group") or "",
              "BEV_APPROVER_GROUP": v.get("approver_group") or ""}
    run(pg, mutate=not DRY, env={k: x for k, x in pg_env.items() if x})
    log(f"trace tables {cat}.{sch}.{v['trace_table_prefix']}_otel_* + experiment {v['mlflow_experiment_id']}: app SP via app resources "
        "(MODIFY on the four trace tables only); viewer group SELECT; experiment ACL set by mlflow_setup/01_experiment.py")


def verify(ctx):
    name = ctx.app_name
    if DRY:
        log(f"DRY-RUN  databricks apps get {name}")
        return
    a = json.loads(run(["databricks", "apps", "get", name, "-p", ctx.profile, "-o", "json"], capture=True, mutate=False))
    scopes = set(a.get("effective_user_api_scopes") or [])
    state = (a.get("app_status") or {}).get("state")
    if REQUIRED_SCOPES - scopes:
        raise SystemExit(f"[deploy] FATAL: scopes {sorted(REQUIRED_SCOPES - scopes)} missing on {name}: {sorted(scopes)}")
    if state != "RUNNING":
        raise SystemExit(f"[deploy] app {name} is {state}: run `databricks apps logs {name} -p {ctx.profile}`")
    log(f"app {name} RUNNING at {a.get('url')} with scopes {sorted(scopes)}")
    sp = a["service_principal_client_id"]
    v = resolved_vars(ctx)
    check_trace_grants(ctx, v, sp)
    # the Apps postgres resource grants CONNECT_AND_CREATE: confirm 52 took CREATE away again after this deploy
    run([sys.executable, "setup/52_lakebase_access.py", "--verify", "--app-sp", sp], mutate=False,
        env={"DATABRICKS_CONFIG_PROFILE": ctx.profile})
    log("next: open the URL (accept the OAuth consent), send one real chat turn, then read `databricks apps logs`")


class Ctx:
    def __init__(self, a):
        self.target, self.profile = a.target, a.profile
        from_env = {k: v for k, v in target_vars().items() if v}
        self.vars = from_env | dict(v.split("=", 1) for v in a.var)
        self.app_name = self.vars["app_name"]


def main():
    global DRY
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", choices=["all", *STEPS])
    p.add_argument("--target", default="dev")
    # same profile as the rest of the repo (.env DATABRICKS_CONFIG_PROFILE), never the CLI DEFAULT
    p.add_argument("--profile", default=os.environ.get("DATABRICKS_CONFIG_PROFILE", "DEFAULT"))
    p.add_argument("--var", action="append", default=[], help="bundle variable key=value (repeatable)")
    p.add_argument("--from", dest="start", choices=STEPS, help="with all: resume from this step")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    DRY = a.dry_run
    ctx = Ctx(a)
    steps = STEPS[STEPS.index(a.start):] if a.step == "all" and a.start else (STEPS if a.step == "all" else [a.step])
    try:
        _run_steps(steps, ctx)
    finally:
        if "deploy" in steps:
            clean()


def _run_steps(steps, ctx):
    for s in steps:
        log(f"== {s}")
        globals()[s](ctx)


if __name__ == "__main__":
    main()
