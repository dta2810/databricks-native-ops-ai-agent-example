"""Offline checks for the operator-side guards: email/tag validation, teardown group ownership, trace grant evaluation,
the Lakebase CREATE checks and the removed full prompt grants."""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "setup"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import config as C  # noqa: E402


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_emails_lowercases_dedupes_and_rejects_bad(monkeypatch):
    monkeypatch.setenv("BEV_PLANNERS", " Ana@Example.com, ana@example.com ,bo@example.org")
    assert C.emails("BEV_PLANNERS") == ["ana@example.com", "bo@example.org"]
    monkeypatch.setenv("BEV_PLANNERS", 'x@example.com" or userName pr "')
    with pytest.raises(SystemExit):
        C.emails("BEV_PLANNERS")
    monkeypatch.delenv("BEV_PLANNERS")
    assert C.emails("BEV_PLANNERS") == []


@pytest.mark.parametrize("tags,ok", [({"bev_project": "bev_ops_x1", "owner": "team a"}, True),
                                     ({"owner": "x'); DROP SCHEMA s; --"}, False), ({"k`": "v"}, False), ({"": "v"}, False)])
def test_tag_validation(tags, ok):
    if ok:
        assert C.validate_tags(tags) == tags
    else:
        with pytest.raises(SystemExit):
            C.validate_tags(tags)


def test_teardown_never_deletes_unproven_groups():
    T = _load("scripts/teardown.py", "teardown_mod")
    assert T.group_is_ours({"1", "2"}, {"1", "2", "3"}) == (True, "")
    assert not T.group_is_ours(set(), {"1"})[0]            # empty group: name alone proves nothing
    assert not T.group_is_ours({"1"}, set())[0]            # no configured members
    assert not T.group_is_ours({"1", "9"}, {"1"})[0]       # a foreign member


def test_trace_grant_violations():
    D = _load("scripts/deploy.py", "deploy_mod")
    allowed = {"bev_ops_viewer_x1", "sp-123", "owner@example.com"}
    ok = [{"Principal": "bev_ops_viewer_x1", "ActionType": "SELECT", "ObjectType": "TABLE", "ObjectKey": "c.s.bev_ops_otel_spans"},
          {"Principal": "sp-123", "ActionType": "MODIFY", "ObjectType": "TABLE", "ObjectKey": "c.s.bev_ops_otel_spans"},
          {"Principal": "bev_ops_planner_x1", "ActionType": "USE SCHEMA", "ObjectType": "SCHEMA", "ObjectKey": "c.s"},
          {"Principal": "account users", "ActionType": "USE CATALOG", "ObjectType": "CATALOG", "ObjectKey": "c"}]
    assert D.trace_grant_violations(ok, allowed) == []
    bad = ok + [{"Principal": "bev_ops_planner_x1", "ActionType": "SELECT", "ObjectType": "TABLE", "ObjectKey": "c.s.bev_ops_otel_logs"},
                {"Principal": "account users", "ActionType": "SELECT", "ObjectType": "CATALOG", "ObjectKey": "c"},
                {"Principal": "`analysts`", "ActionType": "ALL PRIVILEGES", "ObjectType": "SCHEMA", "ObjectKey": "c.s"}]
    v = D.trace_grant_violations(bad, allowed)
    assert len(v) == 3 and any("account users" in x for x in v) and any("analysts" in x for x in v)


def test_trace_prefix_validated():
    D = _load("scripts/deploy.py", "deploy_mod2")
    assert D.trace_prefix({"trace_table_prefix": "bev_ops"}) == "bev_ops"
    with pytest.raises(SystemExit):
        D.trace_prefix({"trace_table_prefix": "x`; DROP"})


def test_prompt_grants_full_option_removed():
    src = open(os.path.join(ROOT, "scripts", "deploy.py")).read()
    assert "--prompt-grants" not in src and '"MANAGE"]' not in src


def test_lakebase_create_checks_cover_public_and_fail_on_true():
    pytest.importorskip("psycopg")
    L = _load("setup/52_lakebase_access.py", "lb_access")
    labels = [l for l, _ in L.create_privilege_checks(True)]
    assert "database" in labels and "schema public" in labels and f"schema {L.STATE}" in labels
    assert L.failing_checks({"database": False, "schema public": True}) == ["schema public"]
    assert L.failing_checks({"database": False}) == []
    stmt = L.sp_public_revoke("sp-1")[0].as_string(None)
    assert stmt.startswith("REVOKE CREATE ON SCHEMA public") and "GRANT" not in stmt


def test_legacy_genie_script_removed():
    assert not os.path.exists(os.path.join(ROOT, "setup", "genie_space.py"))
