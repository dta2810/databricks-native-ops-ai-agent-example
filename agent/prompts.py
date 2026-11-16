"""Prompts are parameterized templates: UC Prompt Registry by alias (mlflow_setup/02_prompts.py) with local files as fallback.
Values come from mlflow_setup/prompt_params.yaml, overridable by env BEV_PROMPT_<NAME> (e.g. BEV_PROMPT_MARKETS)."""
import os
import re
from functools import lru_cache

ROOT = os.path.join(os.path.dirname(__file__), "..")
CATALOG = os.environ.get("BEV_CATALOG", "")
SCHEMA = os.environ.get("BEV_SCHEMA", "")
PROMPT_SCHEMA = os.environ.get("BEV_PROMPT_SCHEMA") or (f"{SCHEMA}_prompts" if SCHEMA else "")
ALIAS = os.environ.get("BEV_PROMPT_ALIAS", "production")
PROMPT_DIR = os.path.join(ROOT, "mlflow_setup", "prompts")
PARAMS_FILE = os.path.join(ROOT, "mlflow_setup", "prompt_params.yaml")


@lru_cache(maxsize=1)
def params():
    vals = {}
    if os.path.exists(PARAMS_FILE):
        import yaml
        vals = {k: str(v) for k, v in (yaml.safe_load(open(PARAMS_FILE)) or {}).items()}
    for k in list(vals):
        vals[k] = os.environ.get(f"BEV_PROMPT_{k.upper()}", vals[k])
    return vals


def _render(template, **extra):
    # MLflow-style {{var}}; unknown vars fail fast
    def sub(m):
        k = m.group(1).strip()
        if k in extra:
            return str(extra[k])
        if k not in params():
            raise KeyError(f"prompt variable {k!r} missing in prompt_params.yaml")
        return params()[k]
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", sub, template)


_FALLBACK_RETRY_S = 60
_fallback_at: dict = {}


def _template(name, file):
    # registry hits are cached for the process; a local-file fallback is retried after a minute
    # (e.g. the app started before its prompt-schema grant): never pin the fallback for the process lifetime
    import time
    if name in _fallback_at and time.time() - _fallback_at[name][0] < _FALLBACK_RETRY_S:
        return _fallback_at[name][1]
    try:
        t = _registry_template(name)
        _fallback_at.pop(name, None)
        return t
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("prompt %s from registry failed (%s); using local file", name, e)
        text = open(os.path.join(PROMPT_DIR, file)).read()
        _fallback_at[name] = (time.time(), text)
        return text


@lru_cache(maxsize=4)
def _registry_template(name):
    import mlflow
    prof = os.environ.get("DATABRICKS_CONFIG_PROFILE")
    mlflow.set_tracking_uri(os.environ.get("BEV_MLFLOW_TRACKING_URI", f"databricks://{prof}" if prof else "databricks"))
    mlflow.set_registry_uri(os.environ.get("BEV_MLFLOW_REGISTRY_URI", f"databricks-uc://{prof}" if prof else "databricks-uc"))
    return mlflow.genai.load_prompt(f"prompts:/{CATALOG}.{PROMPT_SCHEMA}.{name}@{ALIAS}").template


def system_prompt():
    return _render(_template("bev_ops_system", "system.md"))


def router_prompt(message):
    return _render(_template("bev_ops_router", "router.md"), message=message)
