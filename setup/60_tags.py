"""Tag every agent resource we own (idempotent). Tags come from config.TAGS (env BEV_TAGS).
- model services: UC governed tags (entity-tag-assignments) -> service_tags in ai_gateway.usage + billing custom_tags
- Lakebase project: custom_tags -> billing custom_tags
- UC schema: schema tags; MLflow experiment: experiment tags (organization only)
Not taggable here: shared system services (system.ai.dbsql MCP, system.ai.gte-large-en embeddings: request tags only;
gte is not a valid model-service destination in some workspaces), Genie spaces, the shared SQL warehouse.
Usage: python setup/60_tags.py [--check]
"""
import os
import sys

from databricks.sdk.errors import NotFound

import config as C
from sqlrun import client, run

SERVICES = ["llm_fast", "llm_reasoning", "embeddings"]


def _assigned(w, fq):
    out = w.api_client.do("GET", f"/api/2.1/unity-catalog/entity-tag-assignments/model-services/{fq}/tags")
    return {t["tag_key"]: t.get("tag_value") for t in out.get("tag_assignments", [])}


def tag_services(w, check):
    for svc in SERVICES:
        fq = f"{C.CATALOG}.{C.SCHEMA}.{svc}"
        try:
            have = _assigned(w, fq)
        except NotFound:
            print(f"  skip {fq}: service not found (run setup/model_services.py)")
            continue
        for k, v in C.TAGS.items():
            if have.get(k) == v:
                continue
            if check:
                print(f"  MISSING {fq} {k}={v} (has {have.get(k)!r})")
                continue
            if k in have:
                w.api_client.do("DELETE", f"/api/2.1/unity-catalog/entity-tag-assignments/model-services/{fq}/tags/{k}")
            w.api_client.do("POST", "/api/2.1/unity-catalog/entity-tag-assignments",
                            body={"entity_type": "model-services", "entity_name": fq, "tag_key": k, "tag_value": v})
        print(f"  {fq}: {_assigned(w, fq)}")


def tag_lakebase(w, check):
    name = f"projects/{C.LAKEBASE_PROJECT}"
    p = w.api_client.do("GET", f"/api/2.0/postgres/{name}")
    have = {t["key"]: t.get("value") for t in (p.get("status") or {}).get("custom_tags") or []}
    want = have | C.TAGS
    if have != want and not check:
        w.api_client.do("PATCH", f"/api/2.0/postgres/{name}", query={"update_mask": "spec.custom_tags"},
                        body={"spec": {"custom_tags": [{"key": k, "value": v} for k, v in want.items()]}})
        p = w.api_client.do("GET", f"/api/2.0/postgres/{name}")
    print(f"  {name}: {(p.get('status') or {}).get('custom_tags')}")


def tag_schema(check):
    for sch in (C.SCHEMA, C.PROMPT_SCHEMA):
        if not check:
            pairs = ", ".join(f"'{k}' = '{v}'" for k, v in C.validate_tags(C.TAGS).items())  # validated: no quotes reach SQL
            run(f"ALTER SCHEMA {C.CATALOG}.{sch} SET TAGS ({pairs})")
        print(f"  schema {sch}:", run(f"""SELECT tag_name, tag_value FROM system.information_schema.schema_tags
                                  WHERE catalog_name = '{C.CATALOG}' AND schema_name = '{sch}'"""))


def tag_experiment(w, check):
    exp = os.environ.get("BEV_MLFLOW_EXPERIMENT_ID")
    if not exp:
        print("  experiment: BEV_MLFLOW_EXPERIMENT_ID not set")
        return
    if not check:
        for k, v in C.TAGS.items():
            w.experiments.set_experiment_tag(exp, k, v)
    tags = {t.key: t.value for t in (w.experiments.get_experiment(exp).experiment.tags or []) if t.key in C.TAGS}
    print(f"  experiment {exp}: {tags}")


def main(check=False):
    w = client()
    print("tags:", C.TAGS, "(check only)" if check else "")
    for step in (lambda: tag_services(w, check), lambda: tag_lakebase(w, check), lambda: tag_schema(check),
                 lambda: tag_experiment(w, check)):
        try:
            step()
        except Exception as e:  # report and keep going: each resource is independent
            print("  ERROR", type(e).__name__, str(e)[:300])


if __name__ == "__main__":
    main(check="--check" in sys.argv)
