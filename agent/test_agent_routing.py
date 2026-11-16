"""Offline tests: doc-first routing for why/policy questions, and recall with ambiguous product names."""
from contextlib import contextmanager
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

import agent.tools as T
from agent.graph import _doc_first


def test_doc_first_only_for_why_before_any_tool_round():
    msgs = [HumanMessage("Who needs to approve a replenishment request of 800 cases?")]
    assert _doc_first({"intent": "why"}, msgs)
    assert not _doc_first({"intent": "lookup"}, msgs)
    assert not _doc_first(None, msgs)


def test_doc_first_off_after_a_tool_round_this_turn():
    msgs = [HumanMessage("why is Harbor Lager down?"), AIMessage("", tool_calls=[{"name": "search_documents", "args": {}, "id": "1"}])]
    assert not _doc_first({"intent": "why"}, msgs)


def test_doc_first_ignores_tool_rounds_of_earlier_turns():
    msgs = [HumanMessage("earlier"), AIMessage("", tool_calls=[{"name": "stock_cover", "args": {}, "id": "1"}]),
            AIMessage("done"), HumanMessage("who approves 800 cases?")]
    assert _doc_first({"intent": "why"}, msgs)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Conn:
    # product lookups: exact name finds nothing, partial name finds the rows given
    def __init__(self, product_rows):
        self.product_rows = product_rows
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(sql)
        if "ILIKE" in sql:
            return _Rows(self.product_rows)
        return _Rows([])


def test_ambiguous_product_name_becomes_search_text():
    conn = _Conn([("HB-LAGR-355B", "Harbor Lager 355ml Bottle"), ("HB-LAGR-473C", "Harbor Lager 473ml Can")])
    assert T._soft(T._sku_code, conn, "Harbor Lager") == (None, "Harbor Lager")


def test_unique_product_name_resolves_to_sku():
    conn = _Conn([("HB-LAGR-473C", "Harbor Lager 473ml Can")])
    assert T._soft(T._sku_code, conn, "Harbor Lager 473ml Can") == ("HB-LAGR-473C", "")


def test_recall_tool_passes_ambiguous_sku_as_query_text(monkeypatch):
    import memory.decisions as D
    seen = {}

    def fake_recall(conn, user, query=None, market_id=None, sku=None, distributor_id=None, top_k=5, embed_fn=None):
        seen.update(query=query, market_id=market_id, sku=sku, distributor_id=distributor_id)
        return []

    monkeypatch.setattr(D, "recall_decisions", fake_recall)
    conn = _Conn([("HB-LAGR-355B", "Harbor Lager 355ml Bottle"), ("HB-LAGR-473C", "Harbor Lager 473ml Can")])

    @contextmanager
    def pg(role):
        yield conn

    ctx = SimpleNamespace(pg=pg, user_email="planner.fl@acme-bev.example", embed=lambda texts: [[0.0, 0.0, 0.0] for _ in texts])
    tools = {t.name: t for t in T.build_tools(ctx)}
    out = tools["recall_decisions"].invoke({"market": "FL", "sku": "Harbor Lager"})
    assert out
    assert seen == {"query": "Harbor Lager", "market_id": "FL", "sku": None, "distributor_id": None}


def test_recall_description_covers_open_ended_questions():
    tools = {t.name: t for t in T.build_tools(SimpleNamespace())}
    assert "anything I should know" in tools["recall_decisions"].description
    assert "approval rules" in tools["search_documents"].description
