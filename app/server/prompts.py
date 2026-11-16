"""Parameterized text: prompt templates (server/prompts/*.md|yaml, {{vars}}) and the demo stories file (BEV_DEMO_CASES)."""
import re

import yaml
from fastapi import APIRouter, HTTPException

from server import settings as S

router = APIRouter()
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_cache: dict = {}


def _load(path, parse):
    # reload when the file changes (edit demo_cases.yaml without restarting the app)
    mtime = path.stat().st_mtime
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    val = parse(path.read_text())
    _cache[path] = (mtime, val)
    return val


def render(template: str, **params) -> str:
    missing = [v for v in _VAR.findall(template) if v not in params]
    if missing:
        raise KeyError(f"template params missing: {missing}")
    return _VAR.sub(lambda m: str(params[m.group(1)]), template)


def template(name: str) -> str:
    return _load(S.PROMPTS_DIR / name, lambda t: t)


def demo_cases() -> dict:
    return _load(S.DEMO_CASES, yaml.safe_load) or {}


@router.get("/demo-cases")
def get_demo_cases():
    try:
        d = demo_cases()
    except FileNotFoundError:
        raise HTTPException(503, "Demo cases file not found")
    stories = [{"key": k, "title": v.get("title", k), "prompts": v.get("prompts", [])} for k, v in (d.get("stories") or {}).items()]
    return {"company": d.get("company", ""), "product": d.get("product", "Beverage Ops Agent"), "currency": d.get("currency", "USD"),
            "stories": stories, "ask": _load(S.PROMPTS_DIR / "ask_templates.yaml", yaml.safe_load)}
