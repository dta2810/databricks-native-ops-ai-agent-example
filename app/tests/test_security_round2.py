# Offline tests: CSRF guard, turn reservation, trace salt guard, keyed session ids, people masking, amend bounds, manager group.
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from test_api import _load


@pytest.mark.parametrize("method,path,headers,ok", [
    ("GET", "/api/threads", {}, True),
    ("POST", "/assets/x", {}, True),                                                    # not the API
    ("POST", "/api/chat", {"content-type": "application/json"}, True),                  # no provenance headers
    ("POST", "/api/chat", {"content-type": "application/json", "sec-fetch-site": "same-origin"}, True),
    ("DELETE", "/api/threads/x", {"content-type": "application/json", "sec-fetch-site": "same-origin"}, True),
    ("POST", "/api/chat", {"content-type": "application/json", "sec-fetch-site": "cross-site"}, False),
    ("POST", "/api/chat", {"content-type": "text/plain", "sec-fetch-site": "same-origin"}, False),
    ("POST", "/api/chat", {}, False),                                                   # no content type: no-cors trick
    ("POST", "/api/chat", {"content-type": "application/json", "origin": "https://evil.example", "host": "app.example"}, False),
    ("POST", "/api/chat", {"content-type": "application/json", "origin": "https://app.example", "host": "app.example"}, True),
])
def test_csrf_ok_matrix(monkeypatch, method, path, headers, ok):
    app = _load(monkeypatch)
    assert app.csrf_ok(method, path, headers) is ok


def test_csrf_middleware_blocks_cross_site_post(monkeypatch):
    app = _load(monkeypatch)
    c = TestClient(app.app)
    r = c.post("/api/chat", content=b'{"thread_id": "x", "message": "hi"}', headers={"sec-fetch-site": "cross-site", "content-type": "application/json"})
    assert r.status_code == 403
    r = c.post("/api/chat", content=b'{"thread_id": "x", "message": "hi"}', headers={"content-type": "text/plain"})
    assert r.status_code == 403
    assert c.get("/api/health").status_code == 200


def test_reserve_is_exclusive_and_released(monkeypatch):
    _load(monkeypatch)
    from server import chat
    chat._live.clear()
    live = chat._reserve("k")
    with pytest.raises(HTTPException) as e:
        chat._reserve("k")             # second concurrent turn on the same conversation
    assert e.value.status_code == 409
    chat._release("k", live)           # never started: frees the conversation
    assert not chat._busy("k")
    again = chat._reserve("k")
    again.task = object()              # started turns are not released by the guard
    chat._release("k", again)
    assert chat._busy("k")
    chat._live.clear()


def test_chat_route_releases_reservation_on_rate_limit(monkeypatch):
    app = _load(monkeypatch, BEV_CHAT_RATE_PER_MIN="1")
    from server import chat, identity
    from test_api import FakeStore
    monkeypatch.setattr(identity, "_email", lambda r: ("tok", "a@x.com"))
    monkeypatch.setattr(identity, "_roles", lambda t, e: ["g_plan"])
    monkeypatch.setattr(chat, "store", FakeStore())
    chat._live.clear()
    monkeypatch.setattr(chat.chat_limiter, "allow", lambda k: False)
    tid = "22222222-2222-2222-2222-222222222222"
    r = TestClient(app.app).post("/api/chat", json={"thread_id": tid, "message": "hi"})
    assert r.status_code == 429
    assert not chat._live                # the 429 left no reservation behind


def test_salt_required_in_apps(monkeypatch):
    from agent.tracing import require_salt
    monkeypatch.delenv("BEV_TRACE_SALT", raising=False)
    monkeypatch.delenv("BEV_TRACE_MASK_USER", raising=False)
    with pytest.raises(RuntimeError, match="BEV_TRACE_SALT"):
        require_salt(True)
    require_salt(False)                  # local: warning only
    monkeypatch.setenv("BEV_TRACE_MASK_USER", "false")
    require_salt(True)                   # masking off: no salt needed
    monkeypatch.setenv("BEV_TRACE_MASK_USER", "true")
    monkeypatch.setenv("BEV_TRACE_SALT", "s")
    require_salt(True)


def test_app_refuses_to_start_in_apps_without_salt(monkeypatch):
    with pytest.raises(RuntimeError, match="BEV_TRACE_SALT"):
        _load(monkeypatch, DATABRICKS_APP_NAME="app", BEV_TRACE_SALT="")


def test_session_key_is_keyed(monkeypatch):
    import hashlib
    _load(monkeypatch)
    from server import chat
    from server.identity import User
    u = User(email="Ana@Example.com", token="t")
    k1 = chat.agent_thread(u, "tid")
    assert not k1.startswith(hashlib.sha256(b"ana@example.com").hexdigest()[:12])   # not guessable from the email
    assert k1 == chat.agent_thread(User(email="ana@example.com", token="t"), "tid")  # case-insensitive, stable
    monkeypatch.setenv("BEV_TRACE_SALT", "other")
    assert chat.agent_thread(u, "tid") != k1


def test_people_masked_in_tool_output(monkeypatch):
    monkeypatch.setenv("BEV_TRACE_SALT", "s")
    from agent.tools import _people
    out = _people({"requests": [{"requested_by": "Me@x.com", "decided_by": "boss@x.com", "sku": "A"}], "user_id": "n/a"}, "me@x.com")
    r = out["requests"][0]
    assert r["requested_by"] == "you" and r["decided_by"].startswith("a colleague (u-") and "boss" not in json.dumps(out)
    assert out["user_id"] == "n/a" and r["sku"] == "A"


def _amend_tools(monkeypatch, rec):
    import agent.tools as T
    import memory.replenishment as R
    monkeypatch.setattr(T, "sql_call", lambda ctx, sql: {"rows": [[rec]]})
    monkeypatch.setattr(R, "get_request", lambda conn, rid: {"distributor_id": "CA-D1", "sku": "FZ-COLA-600P"})
    monkeypatch.setattr(R, "amend_request", lambda conn, rid, user, qty, reason: {"request_id": rid, "qty_cases": qty, "requested_by": user})

    @contextmanager
    def cm(*a):
        yield None
    ctx = SimpleNamespace(can_propose=True, user_email="p@x.com", write=cm, pg=cm)
    return {t.name: t for t in T.build_tools(ctx)}, T, ctx


def test_amend_fails_closed_without_recommendation(monkeypatch):
    tools, _, _ = _amend_tools(monkeypatch, 0.0)
    with pytest.raises(ValueError, match="recommends 0"):
        tools["amend_request"].func(request_id="33333333-3333-3333-3333-333333333333", qty_cases=20000)


def test_amend_counts_against_turn_cap_and_masks(monkeypatch):
    tools, T, ctx = _amend_tools(monkeypatch, 100.0)
    monkeypatch.setattr(T, "MAX_PROPOSALS_PER_TURN", 1)
    rid = "33333333-3333-3333-3333-333333333333"
    out = json.loads(tools["amend_request"].invoke({"request_id": rid, "qty_cases": 120}))
    assert out["status"] == "amended" and out["request"]["requested_by"] == "you"
    with pytest.raises(ValueError, match="at most 1"):
        tools["amend_request"].func(request_id=rid, qty_cases=110)


def test_amend_needs_propose_role(monkeypatch):
    tools, _, ctx = _amend_tools(monkeypatch, 100.0)
    ctx.can_propose = False
    with pytest.raises(PermissionError):
        tools["amend_request"].func(request_id="33333333-3333-3333-3333-333333333333", qty_cases=110)


def test_manager_group_probed_when_separate(monkeypatch):
    _load(monkeypatch, BEV_MANAGER_GROUP="g_mgr")
    from server import auth, identity
    from server import settings as S
    assert S.MANAGER_GROUP == "g_mgr"
    monkeypatch.setattr(auth, "mint", lambda token, group: None if group == "g_appr" else (_ for _ in ()).throw(PermissionError()))
    me = SimpleNamespace(groups=[SimpleNamespace(display="g_mgr")])
    monkeypatch.setattr(auth, "user_client", lambda t: SimpleNamespace(current_user=SimpleNamespace(me=lambda: me)))
    identity._roles_cache.clear()
    u = identity.User(email="m@x.com", token="t", roles=identity._roles("t", "m@x.com"))
    assert u.can_approve and u.is_manager
    me.groups = []
    identity._roles_cache.clear()
    assert not identity.User(email="n@x.com", token="t", roles=identity._roles("t", "n@x.com")).is_manager


def test_manager_defaults_to_approver(monkeypatch):
    _load(monkeypatch, BEV_MANAGER_GROUP="")
    from server import settings as S
    assert S.MANAGER_GROUP == S.APPROVER_GROUP

