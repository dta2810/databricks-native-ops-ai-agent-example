"""MLflow tracing: one trace per turn (invoke or resume), grouped by session = thread_id, user = email or its hash.
BEV_TRACE_MASK_USER (default true) stores an HMAC-SHA256 prefix of the email instead of the email itself, keyed by the secret
BEV_TRACE_SALT (a Databricks App secret resource). Inside Databricks Apps the app refuses to start when masking is on and
no salt is set (require_salt). Locally (eval, tests) a missing salt falls back to the workspace id with a warning:
that fallback is guessable, so local traces are only pseudonymous.
"""
import hashlib
import hmac
import logging
import os
from contextlib import contextmanager

import mlflow

import agent.context  # noqa: F401  (loads .env for local dev)

_READY = False


def init_tracing():
    global _READY
    if _READY:
        return
    prof = os.environ.get("DATABRICKS_CONFIG_PROFILE")
    # UC-backed trace tables are read through a SQL warehouse
    if os.environ.get("BEV_WAREHOUSE_ID"):
        os.environ.setdefault("MLFLOW_TRACING_SQL_WAREHOUSE_ID", os.environ["BEV_WAREHOUSE_ID"])
    mlflow.set_tracking_uri(os.environ.get("BEV_MLFLOW_TRACKING_URI", f"databricks://{prof}" if prof else "databricks"))
    exp_id = os.environ.get("BEV_MLFLOW_EXPERIMENT_ID")
    target = {"experiment_id": exp_id} if exp_id else {"experiment_name": os.environ.get("BEV_MLFLOW_EXPERIMENT", "/Shared/bev-ops-agent")}
    uc = os.environ.get("BEV_TRACE_UC_SCHEMA")
    if not uc:
        mlflow.set_experiment(**target)
    else:
        from mlflow.entities.trace_location import UCSchemaLocation, UnityCatalog
        cat, sch = uc.split(".", 1)
        exp = mlflow.get_experiment(exp_id) if exp_id else mlflow.get_experiment_by_name(target["experiment_name"])
        linked = (exp.tags if exp else {}).get("mlflow.experiment.databricksTraceDestinationPath")
        if linked == uc:
            # legacy schema-only link (permanent): only the deprecated destination API exports to it
            mlflow.set_experiment(**target)
            mlflow.tracing.set_destination(UCSchemaLocation(catalog_name=cat, schema_name=sch))
        else:
            # current API: experiment-derived UC destination (catalog.schema.<prefix>_otel_*)
            prefix = os.environ.get("BEV_TRACE_TABLE_PREFIX", "bev_ops")
            mlflow.set_experiment(**target, trace_location=UnityCatalog(catalog_name=cat, schema_name=sch, table_prefix=prefix))
    mlflow.langchain.autolog()
    _READY = True


logger = logging.getLogger(__name__)
_WARNED = False


def _mask_on():
    return os.environ.get("BEV_TRACE_MASK_USER", "true").strip().lower() not in ("false", "0", "no")


def require_salt(in_apps):
    """Fail fast in Databricks Apps when masking is on and the secret salt is missing (a guessable salt undoes the mask)."""
    if in_apps and _mask_on() and not os.environ.get("BEV_TRACE_SALT", "").strip():
        raise RuntimeError("BEV_TRACE_SALT is required when BEV_TRACE_MASK_USER is on: bind the trace-salt secret resource "
                           "(resources/app.yml) or set BEV_TRACE_MASK_USER=false")


def _salt():
    global _WARNED
    s = os.environ.get("BEV_TRACE_SALT", "").strip()
    if s:
        return s
    if not _WARNED:
        logger.warning("BEV_TRACE_SALT not set: user ids in traces use the workspace id as key and are guessable")
        _WARNED = True
    return os.environ.get("DATABRICKS_WORKSPACE_ID") or os.environ.get("BEV_WORKSPACE_ID") or ""


def keyed_id(email, n=16):
    """HMAC-SHA256(salt, lowercased email), hex prefix: stable per salt, not reversible without the secret."""
    return hmac.new(_salt().encode(), email.strip().lower().encode(), hashlib.sha256).hexdigest()[:n]


def trace_user(email):
    """Value recorded as the trace user: the email, or 'u-<16 hex>' (stable per salt) when masking is on."""
    if not email or not _mask_on():
        return email
    return "u-" + keyed_id(email)


@contextmanager
def turn_span(name, thread_id, user, kind, inputs):
    # root span of the turn; LangGraph / LLM / tool spans from autolog nest under it
    with mlflow.start_span(name=name, span_type="AGENT") as span:
        span.set_inputs(inputs)
        mlflow.update_current_trace(
            metadata={"mlflow.trace.session": thread_id, "mlflow.trace.user": trace_user(user)},
            tags={"turn_kind": kind, "agent": "bev-ops-agent"},
        )
        yield span
