"""Offline tests: proposal dedupe + per-user pending cap (memory.replenishment) and the per-turn cap (agent tool).
Run: pytest memory/test_proposal_limits.py  (memory/test_memory_local.py is a live script, not a pytest module)"""
import json
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import memory.replenishment as R  # noqa: E402


class _Cur:
    def __init__(self, db):
        self.db, self._one = db, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.db.sql.append(s)
        if "pg_advisory_xact_lock" in s:
            self._one = None
        elif "FROM agent_ops.ref_distributor" in s:
            self._one = {"distributor_id": params[0], "distributor_name": "Dist", "market_id": params[0][:2]}
        elif "FROM agent_ops.ref_product" in s:
            self._one = {"sku": params[0], "product_name": "Prod"}
        elif s.startswith("SELECT count(*)"):
            self._one = {"n": sum(r["status"] == "proposed" and r["requested_by"].lower() == params[0].lower() for r in self.db.rows)}
        elif s.startswith("SELECT") and "status = 'proposed'" in s:
            hit = [r for r in self.db.rows if r["status"] == "proposed" and r["requested_by"].lower() == params[0].lower()
                   and r["distributor_id"] == params[1] and r["sku"] == params[2]]
            self._one = hit[-1] if hit else None
        elif s.startswith("INSERT"):
            row = {"request_id": f"r{len(self.db.rows)}", "created_at": None, "status": "proposed", "requested_by": params[0],
                   "market_id": params[1], "distributor_id": params[2], "sku": params[3], "qty_cases": params[4], "reason": params[5]}
            self.db.rows.append(row)
            self._one = dict(row)

    def fetchone(self):
        return self._one


class _Conn:
    def __init__(self):
        self.rows, self.sql = [], []

    @contextmanager
    def transaction(self):
        yield

    def cursor(self, row_factory=None):
        return _Cur(self)


def test_repeat_proposal_returns_existing():
    c = _Conn()
    a = R.propose_request(c, "p@x.com", "CA-D1", "FZ-COLA-600P", 50, "low cover")
    b = R.propose_request(c, "P@x.com", "CA-D1", "FZ-COLA-600P", 60, "again")
    assert a["duplicate"] is False and b["duplicate"] is True and b["request_id"] == a["request_id"]
    assert len(c.rows) == 1
    assert any("pg_advisory_xact_lock" in s for s in c.sql)


def test_pending_cap_per_user(monkeypatch):
    monkeypatch.setattr(R, "MAX_PENDING_PER_USER", 2)
    c = _Conn()
    R.propose_request(c, "p@x.com", "CA-D1", "FZ-COLA-600P", 50, "r")
    R.propose_request(c, "p@x.com", "CA-D2", "FZ-COLA-600P", 50, "r")
    with pytest.raises(R.ProposalLimit, match="2 open requests"):
        R.propose_request(c, "p@x.com", "CA-D3", "FZ-COLA-600P", 50, "r")
    R.propose_request(c, "q@x.com", "CA-D3", "FZ-COLA-600P", 50, "r")  # other requesters are not affected


def _tools(monkeypatch, rows):
    import agent.tools as T
    monkeypatch.setattr(T, "sql_call", lambda ctx, sql: {"rows": [[100.0]]})
    made = []

    def fake_propose(conn, user, dist, sku, qty, reason, **kw):
        dup = (dist, sku) in made
        made.append((dist, sku))
        return {"request_id": f"r{len(made)}", "distributor_id": dist, "sku": sku, "qty_cases": qty, "duplicate": dup}
    monkeypatch.setattr(R, "propose_request", fake_propose)

    @contextmanager
    def write():
        yield None
    @contextmanager
    def pg(role):
        yield None  # codes match their patterns: no lookup query runs
    ctx = SimpleNamespace(can_propose=True, user_email="p@x.com", write=write, pg=pg)
    return {t.name: t for t in T.build_tools(ctx)}, T


def test_per_turn_cap_and_duplicates_do_not_count(monkeypatch):
    tools, T = _tools(monkeypatch, [])
    monkeypatch.setattr(T, "MAX_PROPOSALS_PER_TURN", 2)
    p = tools["propose_replenishment"]
    args = lambda d: {"distributor_id": d, "sku": "FZ-COLA-600P", "qty_cases": 50, "reason": "r"}  # noqa: E731
    assert json.loads(p.invoke(args("CA-D1")))["status"] == "awaiting_approval"
    assert json.loads(p.invoke(args("CA-D1")))["status"] == "already_open"   # duplicate: not counted
    assert json.loads(p.invoke(args("CA-D2")))["status"] == "awaiting_approval"
    with pytest.raises(ValueError, match="at most 2 new requests"):
        p.func(**args("CA-D3"))
    # a new turn builds new tools: the counter starts again
    fresh, _ = _tools(monkeypatch, [])
    assert json.loads(fresh["propose_replenishment"].invoke(args("CA-D3")))["status"] == "awaiting_approval"


def test_policy_factor_guard_still_applies(monkeypatch):
    tools, _ = _tools(monkeypatch, [])
    with pytest.raises(ValueError, match="exceeds"):
        tools["propose_replenishment"].func(distributor_id="CA-D1", sku="FZ-COLA-600P", qty_cases=151, reason="r")


def test_last_decision_state_is_masked():
    import agent.tools as T
    me = "Ana@Example.com"
    out = T._people({"status": "approved", "decided_by": "ana@example.com",
                     "request": {"requested_by": "bob@example.com", "qty_cases": 50}}, me)
    assert out["decided_by"] == "you"
    assert "@" not in str(out)
