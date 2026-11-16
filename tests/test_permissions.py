"""Offline checks for trace masking, Lakebase SP grants and the identities plan."""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "setup"))


def test_trace_user_masks_by_default(monkeypatch):
    from agent.tracing import trace_user
    monkeypatch.delenv("BEV_TRACE_MASK_USER", raising=False)
    monkeypatch.setenv("BEV_TRACE_SALT", "s1")
    h = trace_user("Ana.Lopez@Example.com")
    assert h.startswith("u-") and len(h) == 18 and "example" not in h
    assert h == trace_user("ana.lopez@example.com")  # case-insensitive, stable
    monkeypatch.setenv("BEV_TRACE_SALT", "s2")
    assert trace_user("ana.lopez@example.com") != h  # salt changes the id


def test_trace_user_unmasked_when_disabled(monkeypatch):
    from agent.tracing import trace_user
    monkeypatch.setenv("BEV_TRACE_MASK_USER", "false")
    assert trace_user("ana@example.com") == "ana@example.com"
    assert trace_user("") == ""


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sp_grants_revoke_create_and_never_public(monkeypatch):
    pytest.importorskip("psycopg")
    import config as C
    for k, v in {"PLANNER_GROUP": "p_t", "APPROVER_GROUP": "a_t"}.items():
        monkeypatch.setattr(C, k, v, raising=False)
    m = _load("setup/52_lakebase_access.py", "lb_access")
    text = [s.as_string(None) for s in m.sp_statements("sp-1", "owner", True)]
    assert any(t.startswith("REVOKE CREATE ON DATABASE") for t in text)
    assert sum(t.startswith("REVOKE CREATE ON SCHEMA") for t in text) == len(m.ALL_SCHEMAS) + 1
    assert not any(t.startswith("GRANT CREATE") or "CREATE ON" in t and t.startswith("GRANT") for t in text)
    assert m.PLANNER == "p_t" and m.APPROVER == "a_t"


def test_identities_plan_and_foreign_members(monkeypatch):
    import config as C
    for k, v in {"PLANNER_GROUP": "bev_ops_planner_t", "APPROVER_GROUP": "bev_ops_approver_t", "VIEWER_GROUP": "bev_ops_viewer_t"}.items():
        monkeypatch.setattr(C, k, v, raising=False)
    monkeypatch.setenv("BEV_PLANNERS", "A@x.com, b@x.com")
    monkeypatch.setenv("BEV_APPROVERS", "b@x.com")
    monkeypatch.delenv("BEV_VIEWERS", raising=False)
    m = _load("setup/identities.py", "identities")
    assert m.plan() == {"bev_ops_planner_t": ["a@x.com", "b@x.com"], "bev_ops_approver_t": ["b@x.com"], "bev_ops_viewer_t": []}
    assert m.foreign_members({"1", "2"}, ["1"]) == {"2"} and not m.foreign_members({"1"}, ["1", "3"])
