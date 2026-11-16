"""Offline: doc_type argument parsing for search_documents (no Lakebase or Gateway)."""
from rag.search import doc_type_filter

ALLOWED = {"promo_brief", "field_report", "policy"}


def test_empty_means_no_filter():
    assert doc_type_filter("", ALLOWED) is None
    assert doc_type_filter(None, ALLOWED) is None


def test_unknown_types_dropped():
    assert doc_type_filter("bogus", ALLOWED) is None
    assert doc_type_filter("policy,bogus", ALLOWED) == ["policy"]


def test_promo_brief_also_reads_field_reports():
    assert doc_type_filter("promo_brief", ALLOWED) == ["promo_brief", "field_report"]
    assert doc_type_filter("promo_brief,field_report", ALLOWED) == ["promo_brief", "field_report"]
    assert doc_type_filter(["field_report"], ALLOWED) == ["field_report"]
