"""Create the LangGraph checkpoint tables (schema BEV_AGENT_STATE_SCHEMA, default agent_state) as the owner. Idempotent.
Usage: .venv/bin/python setup/54_lakebase_agent_state.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import config as C
from databricks.sdk import WorkspaceClient
from databricks_langchain import CheckpointSaver

SCHEMA = os.environ.get("BEV_AGENT_STATE_SCHEMA", "agent_state")
BRANCH = os.environ.get("BEV_LAKEBASE_BRANCH", "production")

with CheckpointSaver(project=C.LAKEBASE_PROJECT, branch=BRANCH, schema=SCHEMA, workspace_client=WorkspaceClient(profile=C.PROFILE)) as saver:
    saver.setup()
print("checkpoint schema ready:", SCHEMA)
