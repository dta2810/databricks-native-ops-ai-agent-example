# Offline tests for the self-approval guard, the chat rate limit and the minimal health endpoint.
import json

import pytest
from fastapi.testclient import TestClient

from test_api import FakeStore, _load


@pytest.mark.parametrize("requested,in_apps,single,expected", [
    (False, False, False, False), (False, True, True, False),
    (True, False, False, True),    # local dev: the flag alone is enough
    (True, True, False, False),    # in Apps without single-presenter mode: ignored
    (True, True, True, True),      # in Apps with both flags
])
def test_self_approval_matrix(monkeypatch, requested, in_apps, single, expected):
    _load(monkeypatch)
    from server.settings import self_approval_effective
    assert self_approval_effective(requested, in_apps, single) is expected


def test_self_approval_ignored_in_apps_without_single_presenter(monkeypatch):
    _load(monkeypatch, BEV_ALLOW_SELF_APPROVAL="true", DATABRICKS_APP_NAME="app")
    from server import settings as S
    assert S.ALLOW_SELF_APPROVAL is False and S.SELF_APPROVAL_IGNORED is True


def test_self_approval_on_in_apps_with_single_presenter(monkeypatch):
    _load(monkeypatch, BEV_ALLOW_SELF_APPROVAL="true", DATABRICKS_APP_NAME="app", BEV_SINGLE_PRESENTER_MODE="1")
    from server import settings as S
    assert S.ALLOW_SELF_APPROVAL is True and S.SELF_APPROVAL_IGNORED is False


def test_token_bucket():
    from server.ratelimit import TokenBucket
    t = {"now": 0.0}
    b = TokenBucket(3, clock=lambda: t["now"])
    assert [b.allow("u") for _ in range(4)] == [True, True, True, False]
    assert b.allow("other")            # buckets are per user
    t["now"] += 20.0                   # 3/min = one token every 20 s
    assert b.allow("u") and not b.allow("u")
    assert all(TokenBucket(0).allow("u") for _ in range(50))  # 0 = no limit


def test_health_is_minimal(monkeypatch):
    app = _load(monkeypatch)
    assert TestClient(app.app).get("/api/health").json() == {"status": "ok"}


def test_me_reports_self_approval_state(monkeypatch):
    app = _load(monkeypatch, BEV_ALLOW_SELF_APPROVAL="true", DATABRICKS_APP_NAME="app")
    from server import identity
    monkeypatch.setattr(identity, "_email", lambda r: ("tok", "a@x.com"))
    monkeypatch.setattr(identity, "_roles", lambda t, e: ["g_plan", "g_appr"])
    me = TestClient(app.app).get("/api/me").json()
    assert me["self_approval"] is False and me["self_approval_ignored"] is True


def test_chat_rate_limit_429(monkeypatch):
    app = _load(monkeypatch, BEV_CHAT_RATE_PER_MIN="1")
    from server import chat, identity
    monkeypatch.setattr(chat, "store", FakeStore())
    chat.start_bridge()
    monkeypatch.setattr(identity, "_email", lambda r: ("tok", "a@x.com"))
    monkeypatch.setattr(identity, "_roles", lambda t, e: ["g_plan"])

    async def fake_run(tid, msg, u):
        yield {"type": "token", "text": "ok"}
        yield {"type": "final"}
    monkeypatch.setattr(chat.bridge, "run", fake_run)
    c = TestClient(app.app)
    with c.stream("POST", "/api/chat", json={"thread_id": "44444444-4444-4444-4444-444444444444", "message": "hi"}) as s:
        assert s.status_code == 200
        [json.loads(l[5:]) for l in s.iter_lines() if l.startswith("data:")]
    r = c.post("/api/chat", json={"thread_id": "55555555-5555-5555-5555-555555555555", "message": "again"})
    assert r.status_code == 429 and "Too many messages" in r.json()["detail"]
