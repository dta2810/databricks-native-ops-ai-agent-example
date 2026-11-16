"""Remove everything one environment created. Default = dry run; --apply deletes.

Order: app (bundle destroy) -> MLflow experiment -> Genie spaces -> UC schema (tables, functions, metric views,
Volume, trace tables, prompts) -> Lakebase project -> account groups. Only this environment's resources are touched:
names carry BEV_ENV, ids come from targets/<target>.resolved.env, groups are deleted only if every member is one of
this environment's configured users.
Usage: BEV_TARGET=<target> python scripts/teardown.py [--steps app,mlflow,genie,schema,lakebase,groups] [--apply]
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "setup"))
import config as C  # noqa: E402

STEPS = ["app", "mlflow", "genie", "schema", "lakebase", "groups"]


def group_is_ours(members, ours):
    """Delete a same-name group only if it has members and every one is a configured user of this environment.
    An empty group, or an environment with no configured users, is never deleted (the name alone proves nothing)."""
    if not ours:
        return False, "no configured members (BEV_PLANNERS/BEV_APPROVERS/BEV_VIEWERS) to prove ownership"
    if not members:
        return False, "group has no members"
    others = set(members) - set(ours)
    if others:
        return False, f"{len(others)} member(s) outside this environment"
    return True, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default=",".join(STEPS))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    steps, do = a.steps.split(","), a.apply
    C.require("CATALOG", "SCHEMA")
    print(f"teardown {'APPLY' if do else 'DRY RUN'} env={C.ENV}: {steps}")
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient(profile=C.PROFILE)

    if "app" in steps:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import deploy as D
        extra = [x for k, v in D.target_vars().items() if v for x in ("--var", f"{k}={v}")]
        cmd = ["databricks", "bundle", "destroy", "--auto-approve", "-t", "dev", "-p", C.PROFILE, *extra]
        print(f"-> bundle destroy (app {C.APP_NAME})")
        if do:
            subprocess.run(cmd, cwd=ROOT, check=True)

    if "mlflow" in steps and C.MLFLOW_EXPERIMENT_ID:
        print(f"-> delete MLflow experiment {C.MLFLOW_EXPERIMENT_ID} (monitors go with it)")
        if do:
            w.experiments.delete_experiment(C.MLFLOW_EXPERIMENT_ID)

    if "genie" in steps:
        for sid in (C.GENIE_COMMERCIAL, C.GENIE_SUPPLY):
            if sid:
                print(f"-> trash Genie space {sid}")
                if do:
                    w.genie.trash_space(sid)

    if "schema" in steps:
        C.require("WAREHOUSE_ID")
        for sch, what in ((C.SCHEMA, "tables, functions, metric views, Volume, trace tables"), (C.PROMPT_SCHEMA, "prompts")):
            print(f"-> DROP SCHEMA {C.CATALOG}.{sch} CASCADE ({what})")
            if do:
                w.statement_execution.execute_statement(warehouse_id=C.WAREHOUSE_ID, wait_timeout="50s",
                                                        statement=f"DROP SCHEMA IF EXISTS {C.CATALOG}.{sch} CASCADE")
        print("   Gateway model services in the schema: check Unity Gateway > Model services and delete any left over")

    if "lakebase" in steps:
        name = f"projects/{C.LAKEBASE_PROJECT}"
        print(f"-> delete Lakebase project {name} (all branches and data)")
        if do and any(p.name == name for p in w.postgres.list_projects()):
            w.postgres.delete_project(name).wait()
        print(f"-> delete secret scope {C.SECRET_SCOPE} (trace salt)")
        if do and C.SECRET_SCOPE in {x.name for x in w.secrets.list_scopes()}:
            w.secrets.delete_scope(C.SECRET_SCOPE)

    if "groups" in steps and C.ACCOUNT_PROFILE:
        from databricks.sdk import AccountClient
        acct = AccountClient(profile=C.ACCOUNT_PROFILE)
        emails = {e for k in ("BEV_PLANNERS", "BEV_APPROVERS", "BEV_VIEWERS") for e in C.emails(k)}
        ours = {u.id for e in emails for u in acct.users.list(filter=f'userName eq "{e}"')}
        for g in (C.PLANNER_GROUP, C.APPROVER_GROUP, C.VIEWER_GROUP):
            for grp in acct.groups.list(filter=f'displayName eq "{g}"'):
                if grp.display_name != g:
                    continue
                members = {m.value for m in (acct.groups.get(grp.id).members or [])}
                ok, why = group_is_ours(members, ours)
                if not ok:
                    print(f"   SKIP account group {g}: {why}")
                    continue
                print(f"-> delete account group {g} ({grp.id})")
                if do:
                    acct.groups.delete(grp.id)
    if not do:
        print("\ndry run: add --apply to delete")


if __name__ == "__main__":
    main()
