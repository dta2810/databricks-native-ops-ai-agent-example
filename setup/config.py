"""Deployment parameters for one target.

Values come from targets/<target>.env (you write it) and targets/<target>.resolved.env (ids the scripts create:
warehouse, Lakebase, Genie spaces, MLflow experiment). Select the target with BEV_TARGET=<target>.
Every resource name carries the BEV_ENV suffix, so several environments can share one workspace and account.
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.environ.get("BEV_TARGET", "")


def _env_file(suffix):
    return os.path.join(ROOT, "targets", f"{TARGET}{suffix}") if TARGET else ""


def _load(path):
    if path and os.path.exists(path):
        for line in open(path):
            line = line.split(" #", 1)[0].strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load(_env_file(".env"))
_load(_env_file(".resolved.env"))


def get(key, default=""):
    return os.environ.get(key) or default


# Required per target: groups and app names are account-/workspace-wide, so pick a suffix nobody else uses
ENV = get("BEV_ENV")
if TARGET and not ENV:
    raise SystemExit(f"set BEV_ENV in targets/{TARGET}.env: a short suffix unique to you (for example your initials + 1)")
ENV = ENV or "local"  # unit tests and local runs only
if not re.fullmatch(r"[a-z0-9]{1,12}", ENV):
    raise SystemExit("BEV_ENV must be 1-12 lowercase letters/digits")

# Databricks workspace
PROFILE = get("DATABRICKS_CONFIG_PROFILE", "DEFAULT")
ACCOUNT_PROFILE = get("BEV_ACCOUNT_PROFILE")
HOST = get("BEV_HOST").rstrip("/")
WORKSPACE_ID = get("BEV_WORKSPACE_ID")
CATALOG = get("BEV_CATALOG")
SCHEMA = get("BEV_SCHEMA", f"bev_ops_{ENV}")
# prompts live in their own schema: the app SP needs CREATE FUNCTION + MANAGE to load them, never on the data schema
PROMPT_SCHEMA = get("BEV_PROMPT_SCHEMA", f"{SCHEMA}_prompts")
WAREHOUSE_ID = get("BEV_WAREHOUSE_ID")
VOLUME = get("BEV_VOLUME", "landing")
APP_NAME = get("BEV_APP_NAME", f"bev-ops-{ENV}")            # Databricks App names are immutable, max 30 chars
LAKEBASE_PROJECT = get("BEV_LAKEBASE_PROJECT", f"bev-ops-{ENV}")
PLANNER_GROUP = get("BEV_PLANNER_GROUP", f"bev_ops_planner_{ENV}")
APPROVER_GROUP = get("BEV_APPROVER_GROUP", f"bev_ops_approver_{ENV}")
VIEWER_GROUP = get("BEV_VIEWER_GROUP", f"bev_ops_viewer_{ENV}")   # reads traces and the MLflow experiment
MANAGER_GROUP = get("BEV_MANAGER_GROUP")                    # optional: approves above the manager threshold (default = approvers)
SECRET_SCOPE = get("BEV_SECRET_SCOPE", f"bev-ops-{ENV}")    # holds the trace salt (setup/trace_salt.py)
GENIE_COMMERCIAL = get("BEV_GENIE_COMMERCIAL")              # resolved by the genie phase
GENIE_SUPPLY = get("BEV_GENIE_SUPPLY")
MLFLOW_EXPERIMENT_ID = get("BEV_MLFLOW_EXPERIMENT_ID")      # resolved by the mlflow phase

# Models: Unity AI Gateway model services created in the schema (setup/model_services.py)
LLM_FAST_MODEL = get("BEV_LLM_FAST_MODEL", "databricks-claude-haiku-4-5")
LLM_FAST_FALLBACK = get("BEV_LLM_FAST_FALLBACK", "databricks-gpt-oss-120b")
LLM_REASONING_MODEL = get("BEV_LLM_REASONING_MODEL", "databricks-claude-sonnet-5")
LLM_REASONING_FALLBACK = get("BEV_LLM_REASONING_FALLBACK", "databricks-claude-sonnet-4-6")
EMBEDDING_MODEL = get("BEV_EMBEDDING_MODEL", "qwen3-embedding-0-6b")
EMBEDDING_SERVICE = get("BEV_EMBEDDING_SERVICE", "system.ai.gte-large-en")
EMBEDDING_DIMS = int(get("BEV_EMBEDDING_DIMS", "1024"))

# publish derived defaults so modules that read env directly (app/server/settings.py, agent/context.py) match in local runs
for _k, _v in {"BEV_SCHEMA": SCHEMA, "BEV_PROMPT_SCHEMA": PROMPT_SCHEMA, "BEV_APP_NAME": APP_NAME, "BEV_LAKEBASE_PROJECT": LAKEBASE_PROJECT,
               "BEV_PLANNER_GROUP": PLANNER_GROUP, "BEV_APPROVER_GROUP": APPROVER_GROUP, "BEV_VIEWER_GROUP": VIEWER_GROUP,
               "BEV_SECRET_SCOPE": SECRET_SCOPE, "BEV_EMBEDDING_SERVICE": EMBEDDING_SERVICE}.items():
    if _v:
        os.environ.setdefault(_k, _v)

# resource tags (service tags -> billing; Lakebase custom tags). Account tag policies may restrict values.
TAGS = json.loads(get("BEV_TAGS", json.dumps({"bev_project": f"bev_ops_{ENV}", "owner": "unset"})))

# one validator for operator-supplied values that reach SQL, SCIM filters or resource names
_PATTERNS = {
    "CATALOG": r"[A-Za-z0-9_]+", "SCHEMA": r"[A-Za-z0-9_]+", "PROMPT_SCHEMA": r"[A-Za-z0-9_]+", "VOLUME": r"[A-Za-z0-9_]+", "WORKSPACE_ID": r"\d+",
    "WAREHOUSE_ID": r"[0-9a-f]+", "APP_NAME": r"[a-z0-9-]{2,30}", "LAKEBASE_PROJECT": r"[a-z0-9-]{1,63}",
    "PLANNER_GROUP": r"[A-Za-z0-9_-]+", "MANAGER_GROUP": r"[A-Za-z0-9_-]+", "SECRET_SCOPE": r"[A-Za-z0-9_.-]+", "APPROVER_GROUP": r"[A-Za-z0-9_-]+", "VIEWER_GROUP": r"[A-Za-z0-9_-]+",
    "GENIE_COMMERCIAL": r"[0-9a-f]+", "GENIE_SUPPLY": r"[0-9a-f]+", "MLFLOW_EXPERIMENT_ID": r"\d+",
}


def validate():
    bad = [k for k, rx in _PATTERNS.items() if globals().get(k) and not re.fullmatch(rx, globals()[k])]
    if bad:
        raise SystemExit(f"invalid settings {bad}: check targets/{TARGET or '<target>'}.env / .resolved.env")


validate()

EMAIL_RX = r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"
TAG_RX = r"[A-Za-z0-9_.:/=+@ -]{1,256}"


def emails(key):
    """Comma-separated emails from env var `key`, lowercased and validated (they reach SCIM filters)."""
    out = sorted({e.strip().lower() for e in get(key).split(",") if e.strip()})
    bad = [e for e in out if not re.fullmatch(EMAIL_RX, e)]
    if bad:
        raise SystemExit(f"{key}: invalid email(s) {bad}")
    return out


def validate_tags(tags):
    """Tag keys/values reach SQL (ALTER ... SET TAGS) and APIs: allow a safe character set only."""
    bad = [f"{k}={v}" for k, v in tags.items() if not (re.fullmatch(TAG_RX, str(k)) and re.fullmatch(TAG_RX, str(v)))]
    if bad:
        raise SystemExit(f"BEV_TAGS: invalid key/value {bad} (allowed: letters, digits, space and _.:/=+@-)")
    return tags


validate_tags(TAGS)


def require(*names):
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise SystemExit(f"missing settings {missing}: set them in targets/{TARGET or '<target>'}.env (see targets/example.env)")


def save_resolved(**values):
    """Persist ids produced by a script into targets/<target>.resolved.env (gitignored) and the current process."""
    if not TARGET:
        print("BEV_TARGET not set: resolved values not saved:", values)
        return
    path = _env_file(".resolved.env")
    cur = {}
    if os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                cur[k] = v
    for k, v in values.items():
        cur[k] = str(v)
        os.environ[k] = str(v)
        globals()[k.replace("BEV_", "", 1)] = str(v)
    validate()
    with open(path, "w") as fh:
        fh.write("# written by the deploy scripts; safe to delete (re-run the steps to rebuild)\n")
        fh.writelines(f"{k}={v}\n" for k, v in sorted(cur.items()))
    print(f"saved {sorted(values)} -> targets/{os.path.basename(path)}")


def params():
    return {"catalog": CATALOG, "schema": SCHEMA, "volume": VOLUME}
