"""Create Unity Gateway model services for the agent (idempotent: creates only if missing)."""
import json

from databricks.sdk.errors import NotFound

import config
from sqlrun import client

SERVICES = {
    "llm_fast": config.LLM_FAST_MODEL,
    "llm_reasoning": config.LLM_REASONING_MODEL,
    "embeddings": config.EMBEDDING_MODEL,
}


def body(model):
    return {"config": {"routing": {"destinations": [{
        "name": f"system.ai.{model}",
        "destination_type": "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL",
        "traffic_percentage": 100,
        "pay_per_token_config": {"model": f"models/system.ai.{model}"},
    }]}}}


def main():
    w = client()
    for svc, model in SERVICES.items():
        fq = f"{config.CATALOG}.{config.SCHEMA}.{svc}"
        try:
            cur = w.api_client.do("GET", f"/api/2.1/unity-catalog/model-services/{fq}")
            dest = [d["name"] for d in cur.get("config", {}).get("routing", {}).get("destinations", [])]
            print(f"exists {fq} -> {dest}")
            continue
        except NotFound:
            pass
        r = w.api_client.do("POST", "/api/2.1/unity-catalog/model-services",
                            query={"parent": f"schemas/{config.CATALOG}.{config.SCHEMA}", "model_service_id": svc},
                            body=body(model))
        print(f"created {fq} -> {model} | api types: {r.get('supported_api_types')}")


if __name__ == "__main__":
    main()
