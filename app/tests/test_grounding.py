import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.grounding import grounded_bullets, ungrounded, source_numbers  # noqa: E402

PAYLOAD = {"as_of_week": "2026-11-02",
           "kpis": [{"label": "Revenue at risk", "value": 1481.37, "delta": -0.047, "subtitle": "TX-D2 VT-ORIG-473C"},
                    {"label": "Positions at risk", "value": 12}],
           "alerts": [{"severity": "high", "title": "PR-0422 below forecast",
                       "body": "CA-D1 uplift -4.7% vs +45% forecast; CA-D3 cover 2.1 days"}]}


def test_invented_range_is_dropped():
    keep, dropped = grounded_bullets([
        "PR-0422 underperforms by 15–50% at CA-D1.",
        "CA-D1 uplift is -4.7% vs +45% forecast; fix the display.",
        "12 positions are at risk, led by TX-D2 with $1,481 weekly revenue at risk.",
        "Replenish CA-D3, at 2.1 days of cover, today."], PAYLOAD)
    assert dropped == ["PR-0422 underperforms by 15–50% at CA-D1."]
    assert len(keep) == 3


def test_codes_and_dates_are_not_figures():
    src = source_numbers(PAYLOAD)
    assert ungrounded("Week of 2026-11-02: TX-D2 VT-ORIG-473C and PR-0422 need action.", src) == []


def test_percent_ratio_and_new_number():
    src = source_numbers(PAYLOAD)
    assert ungrounded("Uplift -4.7% (ratio -0.047 in KPIs).", src) == []
    assert ungrounded("Revenue at risk is $2,900.", src) == [2900.0]
