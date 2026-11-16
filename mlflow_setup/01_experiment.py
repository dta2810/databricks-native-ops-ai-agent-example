"""Create the agent MLflow experiment and link its traces to Unity Catalog with the current API (idempotent).
Link target = UnityCatalog(catalog, schema, table_prefix). A UC link is permanent (the backend rejects unlinking), so an
experiment on the legacy schema-only link (tag databricksTraceDestinationPath == "<catalog>.<schema>") cannot be migrated:
point BEV_MLFLOW_EXPERIMENT at a new name instead.
Access (traces hold questions, user identities and UC rows the tools returned):
  experiment ACL -> CAN_MANAGE for the deployer, CAN_READ for the viewer group (BEV_VIEWER_GROUP); broad grants to
  'users' / 'account users' are removed. Other direct entries (the app SP's CAN_EDIT from its app resource) are kept.
  trace tables   -> SELECT for the viewer group is granted by scripts/deploy.py grants (after the tables exist).
Usage: .venv/bin/python mlflow_setup/01_experiment.py
"""
import os
import sys

import mlflow
from mlflow.entities.trace_location import UnityCatalog

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))
import config as C  # noqa: E402

EXPERIMENT = os.environ.get("BEV_MLFLOW_EXPERIMENT", "")  # default: /Users/<deployer>/bev-ops-agent-traces-<env>
VIEWER = os.environ.get("BEV_VIEWER_GROUP") or C.VIEWER_GROUP
BROAD = {"users", "account users"}
TABLE_PREFIX = os.environ.get("BEV_TRACE_TABLE_PREFIX", "bev_ops")
DEST_TAG = "mlflow.experiment.databricksTraceDestinationPath"


def restrict_access(w, exp_id, deployer):
    from databricks.sdk.service.ml import ExperimentAccessControlRequest, ExperimentPermissionLevel
    cur = w.experiments.get_permissions(exp_id).access_control_list or []
    keep = []
    for e in cur:
        direct = [p for p in (e.all_permissions or []) if not p.inherited]
        if not direct or e.group_name in BROAD or e.group_name == VIEWER or e.user_name == deployer:
            continue
        keep.append(ExperimentAccessControlRequest(user_name=e.user_name, group_name=e.group_name,
                                                   service_principal_name=e.service_principal_name,
                                                   permission_level=direct[0].permission_level))
    acl = keep + [ExperimentAccessControlRequest(user_name=deployer, permission_level=ExperimentPermissionLevel.CAN_MANAGE),
                  ExperimentAccessControlRequest(group_name=VIEWER, permission_level=ExperimentPermissionLevel.CAN_READ)]
    w.experiments.set_permissions(exp_id, access_control_list=acl)
    print("experiment ACL:", [(a.user_name or a.group_name or a.service_principal_name, a.permission_level.value) for a in acl])
    inherited = sorted({e.group_name for e in cur if e.group_name in BROAD and any(p.inherited for p in (e.all_permissions or []))})
    if inherited:
        print(f"WARNING: {inherited} still inherit access from the parent folder: keep the experiment under a private folder")


def main():
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient(profile=C.PROFILE)
    deployer = w.current_user.me().user_name
    global EXPERIMENT
    EXPERIMENT = EXPERIMENT or f"/Users/{deployer}/bev-ops-agent-traces-{C.ENV}"
    mlflow.set_tracking_uri(f"databricks://{C.PROFILE}")
    os.environ.setdefault("MLFLOW_TRACING_SQL_WAREHOUSE_ID", C.WAREHOUSE_ID)
    exp = mlflow.get_experiment_by_name(EXPERIMENT)
    exp_id = exp.experiment_id if exp else mlflow.create_experiment(EXPERIMENT)
    exp = mlflow.get_experiment(exp_id)
    target = UnityCatalog(catalog_name=C.CATALOG, schema_name=C.SCHEMA, table_prefix=TABLE_PREFIX)
    current = exp.tags.get(DEST_TAG)
    if current == f"{C.CATALOG}.{C.SCHEMA}":
        raise SystemExit(f"{EXPERIMENT} uses the legacy schema-only link (permanent); set BEV_MLFLOW_EXPERIMENT to a new name")
    if current and current != target.full_table_prefix:
        raise SystemExit(f"experiment linked to a different location {current!r}; unset it first")
    e = mlflow.set_experiment(experiment_id=exp_id, trace_location=target)
    loc = e.trace_location
    print("experiment", EXPERIMENT, exp_id)
    print("trace location", target.full_table_prefix,
          "| spans", loc.full_otel_spans_table_name if loc else None,
          "| logs", loc.full_otel_logs_table_name if loc else None)
    restrict_access(w, exp_id, deployer)
    C.save_resolved(BEV_MLFLOW_EXPERIMENT_ID=exp_id)
    return exp_id


if __name__ == "__main__":
    main()
