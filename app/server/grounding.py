"""Number grounding for LLM-written text: every figure in the output must appear in the input JSON."""
import re

# codes (TX-D1, VT-ORIG-473C, PR-0422) and ISO dates are identifiers, not figures
_CODE = re.compile(r"\b[A-Z]{1,6}(?:-[A-Z0-9]{1,6})+\b|\b\d{4}-\d{2}-\d{2}\b")
_NUM = re.compile(r"(?<![\w.])[-+−]?\$?\d[\d,]*(?:\.\d+)?\s*[%kKmM]?(?![\w])")


def _parse(tok):
    t = tok.replace("−", "-").replace("$", "").replace(",", "").strip()
    mult, pct = 1.0, False
    if t[-1:] == "%":
        pct, t = True, t[:-1].strip()
    elif t[-1:] in "kK":
        mult, t = 1e3, t[:-1].strip()
    elif t[-1:] in "mM":
        mult, t = 1e6, t[:-1].strip()
    dec = len(t.split(".")[1]) if "." in t else 0
    return float(t) * mult, pct, dec


def numbers_in_text(text):
    clean = _CODE.sub(" ", text or "")
    return [_parse(m.group(0)) for m in _NUM.finditer(clean)]


def _collect(obj, out):
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        for v, _, _ in numbers_in_text(obj):
            out.add(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect(v, out)


def source_numbers(payload):
    out = set()
    _collect(payload, out)
    return out


def _matches(value, pct, dec, sources):
    # same figure at the precision the model wrote it; percent may come as ratio (0.15) or points (15)
    cands = [value]
    if pct:
        cands.append(value / 100)
    for c in cands:
        for s in sources:
            for v in (s, s * 100) if pct else (s,):
                if round(abs(v), dec) == round(abs(c), dec) or abs(abs(v) - abs(c)) < 1e-9:
                    return True
                # model rounded a large figure (1,481.37 -> 1,481) or abbreviated (2.3k)
                if dec == 0 and abs(c) >= 10 and round(abs(v)) == round(abs(c)):
                    return True
                if abs(c) >= 1000 and abs(abs(v) - abs(c)) / abs(c) < 0.005:
                    return True
    return False


def ungrounded(text, sources):
    return [v for v, pct, dec in numbers_in_text(text) if not _matches(v, pct, dec, sources)]


def grounded_bullets(bullets, payload):
    src = source_numbers(payload)
    keep, dropped = [], []
    for b in bullets:
        (dropped if ungrounded(b, src) else keep).append(b)
    return keep, dropped
