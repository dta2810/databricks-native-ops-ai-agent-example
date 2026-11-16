"""Demo clock: the repo holds ONE canonical world (latest data week = CANONICAL_END_WEEK). Before a demo,
scripts/refresh_dates.py shifts every date by whole weeks so the latest week is the last completed week.
delta = max(fact_depletions.week_start) - CANONICAL_END_WEEK; numbers never change, only dates.
Loaders (41 corpus, 51 seeds, eval dataset) render canonical text through shift_text() with the live delta."""
import re
from datetime import date, timedelta

CANONICAL_END_WEEK = date(2026, 11, 2)  # = data/generate.py END_WEEK (Monday)
CANONICAL_YEAR = CANONICAL_END_WEEK.year

# ISO date | ISO week id (2026-W45) | a year token. A bare number is treated as a year ONLY when it is part of an id (after '-',
# e.g. DA-FL-D2-2026) or follows a calendar word (playbook/policy/from/in/of/for/since/until/year/the/Q1-Q4), so quantities like
# "2025 cases" are never touched. Corpus authors: write years in one of those two shapes.
_YEAR_CTX = r"(-|\b(?:[Pp]laybook|[Pp]olicy|[Ff]rom|[Ii]n|[Oo]f|[Ff]or|[Ss]ince|[Uu]ntil|[Yy]ear|[Tt]he|Q[1-4]) )"
_TOKEN = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d])|(?<![\d])(\d{4})-W(\d{2})(?![\d])|"
                    + _YEAR_CTX + r"(202[4-8])(?![\d-])")


def anchor_for(today=None):
    # Monday of the last completed week
    today = today or date.today()
    return today - timedelta(days=today.weekday() + 7)


def current_delta_days(run, w=None):
    # run = setup.sqlrun.run; live shift of the UC data vs the canonical world
    import config as C
    mx = run(f"SELECT max(week_start) FROM {C.CATALOG}.{C.SCHEMA}.fact_depletions", w)[0][0]
    return (date.fromisoformat(str(mx)[:10]) - CANONICAL_END_WEEK).days


def year_offset(delta_days):
    return (CANONICAL_END_WEEK + timedelta(days=delta_days)).year - CANONICAL_YEAR


def shift_text(text, delta_days):
    """Shift canonical dates in text by delta_days (multiple of 7).
    Event dates move by delta; annual bounds (Jan 1 / Dec 31) and standalone years move by the year offset;
    ISO week ids are recomputed from their Monday."""
    if not delta_days or not text:
        return text
    yo = year_offset(delta_days)

    def sub(m):
        if m.group(1):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if (mo, d) in ((1, 1), (12, 31)):
                return f"{y + yo:04d}-{mo:02d}-{d:02d}"
            return (date(y, mo, d) + timedelta(days=delta_days)).isoformat()
        if m.group(4):
            monday = date.fromisocalendar(int(m.group(4)), int(m.group(5)), 1) + timedelta(days=delta_days)
            iy, iw, _ = monday.isocalendar()
            return f"{iy:04d}-W{iw:02d}"
        return m.group(6) + str(int(m.group(7)) + yo)

    return _TOKEN.sub(sub, text)


def shift_obj(obj, delta_days):
    # same as shift_text over every string in a JSON-like structure (eval dataset rows)
    if isinstance(obj, str):
        return shift_text(obj, delta_days)
    if isinstance(obj, list):
        return [shift_obj(x, delta_days) for x in obj]
    if isinstance(obj, dict):
        return {k: shift_obj(v, delta_days) for k, v in obj.items()}
    return obj
