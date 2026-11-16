"""GraphBridge._stream failure paths, offline: retry on a dropped Lakebase connection, error event, stuck-turn timeout."""
import asyncio
import time

import psycopg
import pytest

import agent.bridge as B


class FakeSpan:
    trace_id = "tr-x"

    def __init__(self):
        self.status, self.attrs = None, {}

    def set_attribute(self, k, v):
        self.attrs[k] = v

    def end(self, status=None):
        self.status = status


def _bridge(events_fn):
    b = object.__new__(B.GraphBridge)
    b.inflight, b._saver_obj = {}, None
    b._events = events_fn
    return b


def _collect(b):
    async def go():
        return [e async for e in b._stream("t", None, None, "message", {})]
    return asyncio.run(go())


def test_retries_once_when_lakebase_drops_before_output():
    calls = []

    def events(*a):
        calls.append(1)
        yield {"type": "_trace", "trace_id": f"tr-{len(calls)}"}
        if len(calls) == 1:
            raise psycopg.errors.AdminShutdown("terminating connection due to administrator command")
        yield {"type": "token", "text": "ok"}
    out = _collect(_bridge(events))
    assert len(calls) == 2
    assert [e["type"] for e in out] == ["token", "final"] and out[-1]["trace_id"] == "tr-2"


def test_no_retry_after_output_and_on_other_errors():
    def after_output(*a):
        yield {"type": "token", "text": "partial"}
        raise psycopg.OperationalError("server closed the connection")

    def other(*a):
        raise ValueError("bug")
        yield
    for fn in (after_output, other):
        out = _collect(_bridge(fn))
        assert out[-2]["type"] == "error" and out[-1]["type"] == "final"


def test_stuck_turn_times_out_and_closes_trace(monkeypatch):
    monkeypatch.setattr(B, "TURN_TIMEOUT_S", 0.3)
    span = FakeSpan()

    def stuck(*a):
        yield {"type": "_trace", "trace_id": "tr-stuck", "span": span}
        time.sleep(1.0)  # e.g. a query on a dead connection
        yield {"type": "token", "text": "late"}
    b = _bridge(stuck)
    out = _collect(b)
    assert [e["type"] for e in out] == ["error", "final"] and out[-1]["trace_id"] == "tr-stuck"
    assert span.status == "ERROR" and "bev.abandoned" in span.attrs
    assert b.health()["turns_in_flight"] == 0


@pytest.mark.parametrize("exc,ok", [(psycopg.errors.AdminShutdown("x"), True), (psycopg.InterfaceError("closed"), True),
                                    (ValueError("x"), False)])
def test_is_transient(exc, ok):
    from memory.pgretry import is_transient
    wrapped = RuntimeError("tool failed")
    wrapped.__cause__ = exc
    assert is_transient(exc) is ok and is_transient(wrapped) is ok


def test_retry_once():
    from memory import pgretry
    pgretry.RETRY_DELAY_S = 0
    n = []

    def flaky():
        n.append(1)
        if len(n) == 1:
            raise psycopg.errors.AdminShutdown("x")
        return "ok"
    assert pgretry.retry_once(flaky) == "ok" and len(n) == 2
    with pytest.raises(psycopg.errors.AdminShutdown):
        pgretry.retry_once(lambda: (_ for _ in ()).throw(psycopg.errors.AdminShutdown("again")))
