"""Request tags for Unity AI Gateway calls (land in system.ai_gateway.usage.request_tags; billing uses the service tags).
BEV_REQUEST_TAGS = base tags (JSON), BEV_SURFACE = app | eval | bench | setup."""
import json
import os

BASE = json.loads(os.environ.get("BEV_REQUEST_TAGS", '{"bev_project": "bev_ops_agent"}'))
HEADER = "Databricks-Ai-Gateway-Request-Tags"


def surface():
    return os.environ.get("BEV_SURFACE", "app")


def tags(step):
    return {**BASE, "surface": surface(), "step": step}


def header(step):
    return {HEADER: json.dumps(tags(step), separators=(",", ":"))}
