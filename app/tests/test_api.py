# Offline HTTP tests through the real routers: identity, roles, input validation, thread ownership, resume guard, persistence fold.
import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(1, str(APP.parent))

ENV = {"BEV_CATALOG": "cat", "BEV_SCHEMA": "sch", "BEV_WAREHOUSE_ID": "wh", "BEV_LAKEBASE_PROJECT": "proj",
       "BEV_EMBEDDING_SERVICE": "system.ai.emb", "BEV_PLANNER_GROUP": "g_plan", "BEV_APPROVER_GROUP": "g_appr",
       "BEV_ALLOW_SELF_APPROVAL": "false", "BEV_TRACE_SALT": "test-salt"}


def _load(monkeypatch, **extra):
    monkeypatch.delenv("LOCAL_DEV", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    for k, v in {**ENV, **extra}.items():
        monkeypatch.setenv(k, v)
    for m in [m for m in sys.modules if m == "app" or m.startswith("server")]:
        del sys.modules[m]
    return importlib.import_module("app")


class FakeStore:
    def __init__(self):
        self.threads, self.msgs = {}, []

    def is_uuid(self, v):
        return len(v) == 36

    def get_thread(self, u, tid):
        t = self.threads.get(tid)
        return t if t and t["user_id"] == u.email else None

    def create_thread(self, u, title=None, thread_id=None):
        tid = thread_id or "11111111-1111-1111-1111-111111111111"
        if tid in self.threads:
            return None
        self.threads[tid] = {"thread_id": tid, "user_id": u.email, "title": title or "New conversation"}
        return self.threads[tid]

    def add_user_message(self, u, tid, text):
        self.msgs.append(("user", tid, text))

    def add_assistant_message(self, u, tid, rec):
        self.msgs.append(("assistant", tid, rec))

    def messages(self, u, tid):
        return [m for m in self.msgs if m[1] == tid]

    def pending_approval(self, u, tid):
        return None

    def queue_pending_cards(self, u, tid):
        self.queued = getattr(self, "queued", []) + [tid]

    def get_feedback(self, u, tid, trace_id):
        for m in self.msgs:
            if m[0] == "assistant" and m[1] == tid and m[2].get("trace_id") == trace_id and self.get_thread(u, tid):
                return m[2].get("feedback")
        return None

    def set_card_state_for_request(self, request_id, state, decided_by):
        self.cards = getattr(self, "cards", []) + [(request_id, state, decided_by)]

    def set_feedback(self, u, tid, trace_id, fb):
        for m in self.msgs:
            if m[0] == "assistant" and m[1] == tid and m[2].get("trace_id") == trace_id and self.get_thread(u, tid):
                m[2]["feedback"] = fb
                return True
        return False


@pytest.fixture
def client(monkeypatch):
    app = _load(monkeypatch)
    from server import chat, identity
    store = FakeStore()
    monkeypatch.setattr(chat, "store", store)
    chat.start_bridge()  # lifespan does this in the app; TestClient without `with` skips it
    monkeypatch.setattr(identity, "_email", lambda r: ("tok", r.headers.get("x-user", "a@x.com")))
    monkeypatch.setattr(identity, "_roles", lambda t, e: ["g_plan"] if e.startswith("p") else ["g_plan", "g_appr"])
    return TestClient(app.app), store, chat


def test_required_env_fails_fast(monkeypatch):
    monkeypatch.delenv("BEV_WAREHOUSE_ID", raising=False)
    for k, v in ENV.items():
        if k != "BEV_WAREHOUSE_ID":
            monkeypatch.setenv(k, v)
    for m in [m for m in sys.modules if m.startswith("server")]:
        del sys.modules[m]
    with pytest.raises(RuntimeError, match="BEV_WAREHOUSE_ID"):
        importlib.import_module("server.settings")


def test_local_dev_refused_in_apps(monkeypatch):
    with pytest.raises(RuntimeError, match="LOCAL_DEV"):
        _load(monkeypatch, LOCAL_DEV="true", DATABRICKS_APP_NAME="x")


def test_no_token_is_401(monkeypatch):
    app = _load(monkeypatch)
    assert TestClient(app.app).get("/api/me").status_code == 401


def test_identity_mismatch(monkeypatch):
    app = _load(monkeypatch)
    from server import auth
    monkeypatch.setattr(auth, "user_client", lambda t: type("W", (), {"current_user": type("C", (), {"me": lambda s: type("M", (), {"user_name": "a@x.com"})()})()})())
    r = TestClient(app.app).get("/api/me", headers={"x-forwarded-access-token": "t", "x-forwarded-email": "b@x.com"})
    assert r.status_code == 401


def test_me_roles(client):
    c, _, _ = client
    assert c.get("/api/me", headers={"x-user": "p@x.com"}).json()["can_approve"] is False
    assert c.get("/api/me").json()["can_approve"] is True


@pytest.mark.parametrize("q", [{"market": "gt"}, {"market": "TOOLONG"}, {"threshold_days": 0}, {"threshold_days": 99}])
def test_oos_input_validation(client, q):
    c, _, _ = client
    assert c.get("/api/overview/oos", params=q).status_code == 400


def test_bad_codes_rejected(client):
    c, _, _ = client
    assert c.get("/api/overview/promo", params={"promo_id": "x';drop"}).status_code == 400
    assert c.get("/api/distributors/a b/summary").status_code in (400, 404)


def test_thread_ownership(client):
    c, store, _ = client
    store.create_thread(type("U", (), {"email": "other@x.com"})(), None, "22222222-2222-2222-2222-222222222222")
    r = c.post("/api/chat", json={"thread_id": "22222222-2222-2222-2222-222222222222", "message": "hi"})
    assert r.status_code == 404  # someone else's thread is indistinguishable from missing
    assert c.post("/api/chat", json={"thread_id": "not-a-uuid", "message": "hi"}).status_code == 400


def test_chat_streams_and_persists(client, monkeypatch):
    c, store, chat = client

    async def fake_run(tid, msg, u):
        for e in ({"type": "tool_start", "id": "t1", "tool": "x", "kind": "documents", "label": "L"},
                  {"type": "tool_end", "id": "t1", "ok": True, "sources": [{"kind": "doc", "doc_id": "D1", "section": "S"}, {"kind": "doc", "doc_id": "D1", "section": "S"}]},
                  {"type": "token", "text": "hello"},
                  {"type": "approval_required", "request": {"sku": "A"}}, {"type": "final"}):
            yield e
    monkeypatch.setattr(chat.bridge, "run", fake_run)
    tid = "33333333-3333-3333-3333-333333333333"
    with c.stream("POST", "/api/chat", json={"thread_id": tid, "message": "  hi  "}) as s:
        events = [json.loads(l[5:]) for l in s.iter_lines() if l.startswith("data:")]
    assert [e["type"] for e in events][-1] == "final"
    assert store.msgs[0] == ("user", tid, "hi")
    rec = store.msgs[1][2]
    assert rec["text"] == "hello" and rec["approval"]["state"] == "pending"
    assert rec["sources"] == [{"kind": "doc", "doc_id": "D1", "section": "S"}]  # deduped


def test_new_message_queues_pending_cards_and_folds_submitted(client, monkeypatch):
    c, store, chat = client

    async def fake_run(tid, msg, u):
        for e in ({"type": "token", "text": "sent"},
                  {"type": "requests_submitted", "requests": [{"sku": "A", "request_id": "r1"}, {"sku": "B", "request_id": "r2"}]},
                  {"type": "final"}):
            yield e
    monkeypatch.setattr(chat.bridge, "run", fake_run)
    tid = "55555555-5555-5555-5555-555555555555"
    with c.stream("POST", "/api/chat", json={"thread_id": tid, "message": "propose both"}) as s:
        list(s.iter_lines())
    # a card left pending in this thread moves to the Requests queue before the new turn
    assert store.queued == [tid]
    rec = store.msgs[-1][2]
    assert rec["approval"]["state"] == "submitted" and [r["sku"] for r in rec["approval"]["requests"]] == ["A", "B"]


def test_resume_guards(client):
    c, store, _ = client
    tid = "44444444-4444-4444-4444-444444444444"
    store.create_thread(type("U", (), {"email": "p@x.com"})(), None, tid)
    assert c.post("/api/chat/resume", json={"thread_id": tid, "decision": "approve"}, headers={"x-user": "p@x.com"}).status_code == 403
    assert c.post("/api/chat/resume", json={"thread_id": tid, "decision": "maybe"}).status_code == 400
    store.create_thread(type("U", (), {"email": "a@x.com"})(), None, "55555555-5555-5555-5555-555555555555")
    assert c.post("/api/chat/resume", json={"thread_id": "55555555-5555-5555-5555-555555555555", "decision": "approve"}).status_code == 409


def test_me_with_separate_manager_group(client, monkeypatch):
    # a separate manager group comes back from _roles but is not a key of S.ROLES: /me must not 500
    c, _, _ = client
    from server import identity
    monkeypatch.setattr(identity, "_roles", lambda t, e: ["g_plan", "g_appr", "g_mgr"])
    r = c.get("/api/me")
    assert r.status_code == 200
    assert [x["role"] for x in r.json()["roles"]] == ["g_plan", "g_appr"]


def test_message_length_limit(client):
    c, _, _ = client
    assert c.post("/api/chat", json={"thread_id": "66666666-6666-6666-6666-666666666666", "message": "x" * 4001}).status_code == 422


def test_templates(monkeypatch):
    _load(monkeypatch)
    from server.prompts import render, template
    out = render(template("insights.md"), company="C", currency="USD", as_of_week="2026-11-02", bullets=3)
    assert "{{" not in out and "C" in out
    with pytest.raises(KeyError):
        render("{{a}} {{b}}", a=1)


def test_demo_cases(client):
    c, _, _ = client
    d = c.get("/api/demo-cases").json()
    assert d["stories"] and all(s["prompts"] for s in d["stories"])
    assert "replenish" in d["ask"]


def test_spa_path_containment(client):
    c, _, _ = client
    r = c.get("/..%2F..%2Fapp.py")
    assert r.status_code in (200, 404) and "fastapi" not in r.text.lower()


def test_feedback_logged_and_scoped(client, monkeypatch):
    c, store, chat = client
    tid, logged = "44444444-4444-4444-4444-444444444444", []

    async def fake_run(t, msg, u):
        for e in ({"type": "token", "text": "ok"}, {"type": "final", "trace_id": "tr-abc"}):
            yield e
    monkeypatch.setattr(chat.bridge, "run", fake_run)
    monkeypatch.setattr(chat.bridge, "feedback", lambda *a: logged.append(a), raising=False)
    with c.stream("POST", "/api/chat", json={"thread_id": tid, "message": "hi"}) as s:
        list(s.iter_lines())
    assert store.msgs[1][2]["trace_id"] == "tr-abc"
    assert c.post("/api/chat/feedback", json={"thread_id": tid, "trace_id": "tr-abc", "rating": "meh"}).status_code == 400
    assert c.post("/api/chat/feedback", json={"thread_id": tid, "trace_id": "tr-other", "rating": "up"}).status_code == 404
    assert c.post("/api/chat/feedback", json={"thread_id": tid, "trace_id": "bad id;", "rating": "up"}).status_code == 422
    r = c.post("/api/chat/feedback", json={"thread_id": tid, "trace_id": "tr-abc", "rating": "down", "comment": " wrong SKU "})
    assert r.status_code == 200 and logged == [("tr-abc", False, "wrong SKU", "a@x.com", None)]
    # another user cannot rate it
    r = c.post("/api/chat/feedback", headers={"x-user": "b@x.com"}, json={"thread_id": tid, "trace_id": "tr-abc", "rating": "up"})
    assert r.status_code == 404


def test_feedback_needs_live_agent(client):
    c, _, _ = client
    r = c.post("/api/chat/feedback", json={"thread_id": "55555555-5555-5555-5555-555555555555", "trace_id": "tr-x", "rating": "up"})
    assert r.status_code == 409


RID = "66666666-6666-6666-6666-666666666666"


def _pending(chat, monkeypatch, row):
    # graph bridge waiting on RID; the stored request row is `row`
    monkeypatch.setattr(chat.bridge, "pending_approval", lambda atid: {"request_id": RID}, raising=False)
    monkeypatch.setattr(chat, "_request_row", lambda rid: row)


def test_resume_blocks_self_approval(client, monkeypatch):
    c, store, chat = client
    tid = "77777777-7777-7777-7777-777777777777"
    store.create_thread(type("U", (), {"email": "a@x.com"})(), None, tid)
    _pending(chat, monkeypatch, {"request_id": RID, "requested_by": "A@x.com", "status": "proposed", "qty_cases": 100})
    r = c.post("/api/chat/resume", json={"thread_id": tid, "decision": "approve"})
    assert r.status_code == 403 and "another approver" in r.json()["detail"]


def test_resume_manager_rule(client, monkeypatch):
    c, store, chat = client
    tid = "88888888-8888-8888-8888-888888888888"
    store.create_thread(type("U", (), {"email": "a@x.com"})(), None, tid)
    monkeypatch.setattr(chat.S, "MANAGER_GROUP", "g_mgr")
    _pending(chat, monkeypatch, {"request_id": RID, "requested_by": "p@x.com", "status": "proposed", "qty_cases": 900})
    assert c.post("/api/chat/resume", json={"thread_id": tid, "decision": "approve"}).status_code == 403
    # rejecting never needs a manager: passes the guard (stub has nothing pending -> it re-arms or 409, not 403)
    assert c.post("/api/chat/resume", json={"thread_id": tid, "decision": "reject"}).status_code != 403


def test_decision_route_ownership_and_roles(client, monkeypatch):
    c, store, chat = client
    import memory.replenishment as R
    from server import auth
    from contextlib import contextmanager
    calls = []

    @contextmanager
    def svc():
        yield None
    monkeypatch.setattr(auth, "service_connection", svc)
    monkeypatch.setattr(R, "approve_request", lambda conn, rid, who, reason, **kw: calls.append((rid, who, kw)) or
                        {"status": "approved", "request_id": rid, "decided_by": who})
    rows = {"own": {"request_id": RID, "requested_by": "a@x.com", "status": "proposed", "qty_cases": 10},
            "other": {"request_id": RID, "requested_by": "p@x.com", "status": "proposed", "qty_cases": 10},
            "done": {"request_id": RID, "requested_by": "p@x.com", "status": "approved", "qty_cases": 10}}
    url = f"/api/requests/{RID}/decision"
    monkeypatch.setattr(chat, "_request_row", lambda rid: rows["own"])
    assert c.post(url, json={"decision": "approve"}).status_code == 403            # requester
    monkeypatch.setattr(chat, "_request_row", lambda rid: rows["other"])
    assert c.post(url, headers={"x-user": "p2@x.com"}, json={"decision": "approve"}).status_code == 403  # planner only
    assert c.post("/api/requests/not-a-uuid/decision", json={"decision": "approve"}).status_code == 400
    assert c.post(url, json={"decision": "maybe"}).status_code == 400
    r = c.post(url, json={"decision": "approve", "reason": " ok "})
    assert r.status_code == 200 and calls[-1][1] == "a@x.com" and calls[-1][2]["allow_self_approval"] is False
    assert store.cards[-1] == (RID, "approved", "a@x.com")
    monkeypatch.setattr(chat, "_request_row", lambda rid: rows["done"])
    assert c.post(url, json={"decision": "approve"}).status_code == 409


def test_feedback_rerate_overrides(client, monkeypatch):
    c, store, chat = client
    tid, seen = "99999999-9999-9999-9999-999999999999", []

    async def fake_run(t, msg, u):
        for e in ({"type": "token", "text": "ok"}, {"type": "final", "trace_id": "tr-1"}):
            yield e
    monkeypatch.setattr(chat.bridge, "run", fake_run)

    def fb(trace_id, value, comment, email, assessment_id=None):
        seen.append(assessment_id)
        return assessment_id or "a-1"
    monkeypatch.setattr(chat.bridge, "feedback", fb, raising=False)
    with c.stream("POST", "/api/chat", json={"thread_id": tid, "message": "hi"}) as s:
        list(s.iter_lines())
    assert c.post("/api/chat/feedback", json={"thread_id": tid, "trace_id": "tr-1", "rating": "up"}).status_code == 200
    assert c.post("/api/chat/feedback", json={"thread_id": tid, "trace_id": "tr-1", "rating": "down"}).status_code == 200
    assert seen == [None, "a-1"]  # second rating overrides the first assessment
    assert store.msgs[1][2]["feedback"]["rating"] == "down" and store.msgs[1][2]["feedback"]["assessment_id"] == "a-1"


def test_feedback_down_then_comment(client, monkeypatch):
    # UI records 👎 on click, then the optional comment updates the same assessment (NY-D2 bug: the 👎 was never sent)
    c, store, chat = client
    tid, seen = "a9999999-9999-9999-9999-999999999999", []

    async def fake_run(t, msg, u):
        for e in ({"type": "token", "text": "ok"}, {"type": "final", "trace_id": "tr-2"}):
            yield e
    monkeypatch.setattr(chat.bridge, "run", fake_run)

    def fb(trace_id, value, comment, email, assessment_id=None):
        seen.append((value, comment, assessment_id))
        return assessment_id or "a-9"
    monkeypatch.setattr(chat.bridge, "feedback", fb, raising=False)
    with c.stream("POST", "/api/chat", json={"thread_id": tid, "message": "hi"}) as s:
        list(s.iter_lines())
    for body in ({"rating": "up"}, {"rating": "down"}, {"rating": "down", "comment": "took too much time"}):
        assert c.post("/api/chat/feedback", json={"thread_id": tid, "trace_id": "tr-2", **body}).status_code == 200
    assert seen == [(True, None, None), (False, None, "a-9"), (False, "took too much time", "a-9")]
    assert store.msgs[1][2]["feedback"] == {"rating": "down", "comment": "took too much time", "assessment_id": "a-9"}


TID = "44444444-4444-4444-4444-444444444444"


def test_disconnect_mid_turn_keeps_running_and_persists(client):
    # client leaves after the first event (switch conversation); the turn still finishes and is stored
    import asyncio
    c, store, chat = client
    u = type("U", (), {"email": "a@x.com"})()

    async def slow():
        yield {"type": "tool_start", "id": "g1", "tool": "ask_commercial", "kind": "genie", "label": "Genie"}
        await asyncio.sleep(0.05)
        yield {"type": "tool_end", "id": "g1", "ok": True}
        yield {"type": "token", "text": "California"}
        yield {"type": "final", "trace_id": "tr-1"}

    async def main():
        resp = chat._start(u, TID, "k1", chat._reserve("k1"), slow())
        it = resp.body_iterator
        first = await it.__anext__()
        assert '"tool_start"' in first
        await it.aclose()  # disconnect
        assert chat._busy("k1")
        await chat._live["k1"].task
        assert not chat._busy("k1")
        # a re-attach inside the grace window replays the whole turn
        replay = [x async for x in chat._follow(chat._live["k1"]).body_iterator]
        assert [json.loads(x[6:])["type"] for x in replay] == ["tool_start", "tool_end", "token", "final"]

    asyncio.run(main())
    rec = [m for m in store.msgs if m[0] == "assistant"][0][2]
    assert rec["text"] == "California" and rec["trace_id"] == "tr-1" and rec["tools"][0]["status"] == "ok"


def test_second_message_while_running_is_409(client):
    c, store, chat = client
    store.create_thread(type("U", (), {"email": "a@x.com"})(), None, TID)
    live = chat._Live()
    key = chat.agent_thread(type("U", (), {"email": "a@x.com"})(), TID)
    chat._live[key] = live
    try:
        r = c.post("/api/chat", json={"thread_id": TID, "message": "again"})
        assert r.status_code == 409 and "Still answering" in r.json()["detail"]
        assert not [m for m in store.msgs if m[0] == "user"]  # the duplicate is not stored
        assert c.get(f"/api/threads/{TID}/messages").json()["running"] is True
        r = c.post("/api/chat/resume", json={"thread_id": TID, "decision": "approve"})
        assert r.status_code == 409
    finally:
        chat._live.pop(key, None)


def test_attach_without_live_turn_is_204(client):
    c, store, chat = client
    store.create_thread(type("U", (), {"email": "a@x.com"})(), None, TID)
    assert c.get(f"/api/chat/stream/{TID}").status_code == 204
    assert c.get(f"/api/threads/{TID}/messages").json()["running"] is False
    # not your conversation: same 404 as any other thread route
    assert c.get(f"/api/chat/stream/{TID}", headers={"x-user": "b@x.com"}).status_code == 404


def test_api_docs_off_in_apps(monkeypatch):
    # deployed: no interactive docs or OpenAPI schema; local keeps them
    app = _load(monkeypatch, DATABRICKS_APP_NAME="bev-ops-agent")
    c = TestClient(app.app)
    for path in ("/openapi.json", "/docs", "/redoc"):
        r = c.get(path)
        assert '"openapi"' not in r.text and "swagger-ui" not in r.text.lower(), path
    app = _load(monkeypatch)
    assert '"openapi"' in TestClient(app.app).get("/openapi.json").text
