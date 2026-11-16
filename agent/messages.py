"""Message helpers: user-facing text from AI messages (Claude reasoning blocks stay in state, never shown)."""
import json


def _blocks_text(blocks):
    return "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")


def text_of(msg):
    c = getattr(msg, "content", msg)
    if isinstance(c, list):
        return _blocks_text(c)
    if isinstance(c, str) and c.lstrip().startswith('[{"type"'):
        # serialized content blocks (reasoning + text) leaking as a string
        try:
            return _blocks_text(json.loads(c))
        except ValueError:
            pass
        out, dec, s = [], json.JSONDecoder(), c.strip()
        i = 0
        while i < len(s):
            try:
                obj, j = dec.raw_decode(s, i)
            except ValueError:
                break
            out.append(_blocks_text(obj) if isinstance(obj, list) else "")
            i = j
        return "".join(out)
    return c or ""
