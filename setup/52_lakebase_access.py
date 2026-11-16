"""Lakebase access for the agent app. Model: groups READ, the app SP WRITES.
One Postgres role per Databricks group; the app mints group-scoped credentials from the user's forwarded token for reads.
Every write (chats, proposals, decisions, decision memory) goes through the app SP after the app checked the caller's group,
so a user who mints a group credential and connects directly can neither read other people's chats nor forge decisions.
  planner / approver -> SELECT on shared data only (rag corpus, reference tables, request queue, team decision memory); no agent_ui
  app SP (--app-sp)  -> DML on agent_ui, agent_ops.replenishment_requests, agent_mem.decision_log, agent_state (checkpointer);
                        SELECT on reference tables. No CREATE: the owner runs 50/53/54 setup, and the app never creates
                        objects at runtime. The Apps postgres resource only offers CAN_CONNECT_AND_CREATE, so this script
                        revokes CREATE on the database and on every app schema from the SP role (re-run after each deploy).
Never grants on `public` (it only REVOKEs CREATE there from the SP role), never DATABRICKS_SUPERUSER.
Default = dry run (prints SQL). --apply creates roles for groups that EXIST and runs grants.
--verify (with --app-sp) checks, read-only, that the SP role has no CREATE on the database, the app schemas or public;
exit 1 if it does (scripts/deploy.py verify calls it after every deploy).
Usage: python3 setup/52_lakebase_access.py [--apply | --verify] [--app-sp <sp-application-id>]   (run after 50, 53 and 54)
Env: BEV_APP_SP (alternative to --app-sp), BEV_PLANNER_GROUP / BEV_APPROVER_GROUP (default: setup/config.py, suffixed with BEV_ENV),
     BEV_AGENT_STATE_SCHEMA (checkpoint schema, default agent_state). Profile/project come from DATABRICKS_CONFIG_PROFILE / .env.
"""
import os
import sys

from psycopg import sql

sys.path.insert(0, os.path.dirname(__file__))
import config as C
from lakebase_conn import BRANCH, DATABASE, connect
from sqlrun import client

PLANNER = C.PLANNER_GROUP  # config reads BEV_PLANNER_GROUP (env or target file)
APPROVER = C.APPROVER_GROUP
STATE = os.environ.get("BEV_AGENT_STATE_SCHEMA", "agent_state")
READ_SCHEMAS = ["agent_mem", "agent_ops", "rag"]
ALL_SCHEMAS = READ_SCHEMAS + ["agent_ui"]
GROUP_READ = ["agent_ops.ref_distributor", "agent_ops.ref_product", "agent_ops.replenishment_requests",
              "agent_mem.decision_log", "rag.doc_chunks"]
SP_WRITE = {"agent_ui.threads": "SELECT, INSERT, UPDATE", "agent_ui.messages": "SELECT, INSERT, UPDATE",
            "agent_ops.replenishment_requests": "SELECT, INSERT, UPDATE", "agent_mem.decision_log": "SELECT, INSERT"}
SP_READ = ["agent_ops.ref_distributor", "agent_ops.ref_product"]


def _tables(names):
    return sql.SQL(", ").join(sql.SQL(".").join(map(sql.Identifier, n.split("."))) for n in names)


def statements(groups, state_schema_exists):
    I = sql.Identifier
    out = []
    for g in groups:
        G = I(g)
        out += [sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(I(DATABASE), G)]
        # revoke first so a re-run converges to exactly this spec (also removes the old INSERT/UPDATE grants)
        for s in ALL_SCHEMAS:
            out += [sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(I(s), G)]
        out += [sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(I("agent_ui"), G)]
        out += [sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(I(s), G) for s in READ_SCHEMAS]
        out += [sql.SQL("GRANT SELECT ON {} TO {}").format(_tables(GROUP_READ), G)]
        if state_schema_exists:
            out += [sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(I(STATE), G),
                    sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(I(STATE), G),
                    sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(I(STATE), G)]
    _no_public(out)
    return out


def sp_statements(sp, owner, state_schema_exists):
    I = sql.Identifier
    S = I(sp)
    out = [sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(I(DATABASE), S),
           # the Apps resource grants connect-and-create; the app needs only connect + DML (TODO(verify-live): a redeploy may re-grant CREATE)
           sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(I(DATABASE), S)]
    out += [sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(I(s), S) for s in ALL_SCHEMAS]
    out += [sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(I(s), S) for s in ALL_SCHEMAS]
    for t, privs in SP_WRITE.items():
        out += [sql.SQL("GRANT " + privs + " ON {} TO {}").format(_tables([t]), S)]
    out += [sql.SQL("GRANT SELECT ON {} TO {}").format(_tables(SP_READ), S)]
    if state_schema_exists:
        # checkpointer: DML on its tables + future tables created by the owner's setup() migrations
        out += [sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(I(STATE), S),
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(I(STATE), S),
                sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(I(STATE), S),
                sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(I(STATE), S),
                sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(I(owner), I(STATE), S),
                sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT USAGE, SELECT ON SEQUENCES TO {}").format(I(owner), I(STATE), S)]
    _no_public(out)
    return out


def sp_public_revoke(sp):
    # the one statement allowed to name schema public: it only takes CREATE away from the app SP role
    return [sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(sql.Identifier(sp))]


def create_privilege_checks(state_schema_exists):
    """(label, SQL) pairs; each returns one boolean = the SP role can CREATE there. Parameter: %(role)s."""
    schemas = ALL_SCHEMAS + ([STATE] if state_schema_exists else []) + ["public"]
    checks = [("database", "SELECT has_database_privilege(%(role)s, current_database(), 'CREATE')")]
    checks += [(f"schema {sc}", f"SELECT has_schema_privilege(%(role)s, '{sc}', 'CREATE')") for sc in schemas]
    return checks


def failing_checks(results):
    """results: {label: bool} -> labels where the SP can CREATE (pure, unit-tested)."""
    return sorted(label for label, can in results.items() if can)


def verify(app_sp):
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_sp,)).fetchone():
            raise SystemExit(f"app SP role {app_sp} missing: deploy the app first")
        state_exists = bool(conn.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (STATE,)).fetchone())
        results = {}
        for label, q in create_privilege_checks(state_exists):
            try:
                results[label] = bool(conn.execute(q, {"role": app_sp}).fetchone()[0])
            except Exception as e:  # schema not created yet: nothing to create in
                print(f"-- skip {label}: {str(e).splitlines()[0][:120]}")
                conn.rollback()
    bad = failing_checks(results)
    if bad:
        raise SystemExit(f"app SP {app_sp} can CREATE on {bad}: re-run `./deploy.sh <target> access --apply` (or `app --apply`)")
    print(f"ok  app SP {app_sp}: no CREATE on database, app schemas or public ({len(results)} checks)")


def _no_public(stmts):
    for s in stmts:
        if "public" in s.as_string(None).lower().replace('"', ""):
            raise RuntimeError("refusing to grant on schema public")


def main(apply=False, app_sp=None):
    w = client()
    existing = {g.display_name for g in w.groups.list(attributes="displayName")}
    groups = [g for g in (PLANNER, APPROVER) if g in existing]
    missing = [g for g in (PLANNER, APPROVER) if g not in existing]
    with connect() as conn:
        state_exists = bool(conn.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (STATE,)).fetchone())
        stmts = statements([PLANNER, APPROVER] if not apply else groups, state_exists)
        if app_sp:
            if not state_exists:
                msg = f"{STATE} schema missing: run setup/54_lakebase_agent_state.py (owner) first; the checkpointer grants are skipped"
                if apply:
                    raise SystemExit(msg)
                print(f"-- WARNING: {msg}")
            owner = None
            if state_exists:
                owner = conn.execute("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = %s", (STATE,)).fetchone()[0].strip('"')
            stmts += sp_statements(app_sp, owner, state_exists) + sp_public_revoke(app_sp)
        for s in stmts:
            print(s.as_string(conn) + ";")
        print(f"-- groups present: {groups}; missing: {missing}; {STATE} schema exists: {state_exists}; app SP: {app_sp or '-'}")
        if not apply:
            print("-- dry run (no changes). Re-run with --apply.")
            return
        if not groups and not app_sp:
            print("-- nothing to apply: create the Databricks groups first")
            return
        if app_sp and not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_sp,)).fetchone():
            raise SystemExit(f"app SP role {app_sp} missing: deploy the app first (its postgres resource creates the role)")
        from databricks.sdk.service.postgres import Role, RoleAuthMethod, RoleIdentityType, RoleRoleSpec
        branch = f"projects/{C.LAKEBASE_PROJECT}/branches/{BRANCH}"
        for g in groups:
            try:
                w.postgres.create_role(parent=branch, role=Role(spec=RoleRoleSpec(
                    postgres_role=g, identity_type=RoleIdentityType.GROUP, auth_method=RoleAuthMethod.LAKEBASE_OAUTH_V1)))
                print(f"role created: {g}")
            except Exception as e:
                if "exist" not in str(e).lower():
                    raise
                print(f"role exists: {g}")
        with conn.transaction():
            for s in stmts:
                conn.execute(s)
        print(f"access applied for {groups}" + (f" + app SP {app_sp}" if app_sp else ""))


if __name__ == "__main__":
    a = sys.argv
    sp = a[a.index("--app-sp") + 1] if "--app-sp" in a else os.environ.get("BEV_APP_SP")
    if "--verify" in a:
        if not sp:
            raise SystemExit("--verify needs --app-sp <sp-application-id>")
        verify(sp)
    else:
        main(apply="--apply" in a, app_sp=sp)
