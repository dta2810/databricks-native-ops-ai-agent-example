"""Register the agent prompt TEMPLATES in the UC Prompt Registry and point the alias at the latest version.
Idempotent: a new version only when the template text changes. Variables ({{...}}) are filled at runtime from
mlflow_setup/prompt_params.yaml (env BEV_PROMPT_<KEY> overrides); {{message}} is filled per turn by the agent.
Usage: .venv/bin/python mlflow_setup/02_prompts.py
"""
import os
import re
import sys

import mlflow

sys.path.insert(0, os.path.dirname(__file__))
import params as P  # noqa: E402
from params import C  # noqa: E402


def ensure_prompt_schema():
    # dedicated prompt schema (no tables): the app SP gets CREATE FUNCTION + MANAGE here only (scripts/deploy.py grants)
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient(profile=C.PROFILE)
    fq = f"{C.CATALOG}.{C.PROMPT_SCHEMA}"
    try:
        w.schemas.get(fq)
    except Exception:
        w.schemas.create(name=C.PROMPT_SCHEMA, catalog_name=C.CATALOG, comment="bev-ops agent prompts (Prompt Registry only)")
        print(f"created schema {fq}")


def main():
    ensure_prompt_schema()
    mlflow.set_tracking_uri(f"databricks://{C.PROFILE}")
    mlflow.set_registry_uri(f"databricks-uc://{C.PROFILE}")
    for name, fname in P.FILES.items():
        text = open(os.path.join(P.HERE, "prompts", fname)).read().strip()
        try:
            current = mlflow.genai.load_prompt(f"prompts:/{name}@{P.ALIAS}")
        except Exception:
            current = None
        if current is not None and current.template.strip() == text:
            print(f"{name}@{P.ALIAS} unchanged (v{current.version})")
            continue
        p = mlflow.genai.register_prompt(name=name, template=text, commit_message=f"from mlflow_setup/prompts/{fname}",
                                         tags={"agent": "bev-ops-agent"})
        mlflow.genai.set_prompt_alias(name=name, alias=P.ALIAS, version=p.version)
        print(f"{name} v{p.version} -> @{P.ALIAS}")
    # verify: load by alias and format with params (no unfilled variables left)
    params = P.prompt_params()
    for name in P.FILES:
        p = mlflow.genai.load_prompt(f"prompts:/{name}@{P.ALIAS}")
        out = p.format(**params, message="test") if "{{message}}" in p.template else p.format(**params)
        left = re.findall(r"{{\s*\w+\s*}}", out)
        print("load ok", f"prompts:/{name}@{P.ALIAS}", f"v{p.version}", "vars", sorted(p.variables), "unfilled", left)


if __name__ == "__main__":
    main()
