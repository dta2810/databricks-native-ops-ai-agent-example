"""Prompt registry names + template params (shared by mlflow_setup and the agent)."""
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))
import config as C  # noqa: E402

HERE = os.path.dirname(__file__)
ALIAS = os.environ.get("BEV_PROMPT_ALIAS", "production")
SYSTEM = f"{C.CATALOG}.{C.PROMPT_SCHEMA}.bev_ops_system"
ROUTER = f"{C.CATALOG}.{C.PROMPT_SCHEMA}.bev_ops_router"
FILES = {SYSTEM: "system.md", ROUTER: "router.md"}


def prompt_params():
    with open(os.path.join(HERE, "prompt_params.yaml")) as fh:
        p = yaml.safe_load(fh)
    return {k: os.environ.get(f"BEV_PROMPT_{k.upper()}", str(v)) for k, v in p.items()}
