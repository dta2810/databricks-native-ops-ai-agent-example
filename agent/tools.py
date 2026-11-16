"""Agent tools, built per request with the user's context (OBO). SQL is assembled in code from validated arguments."""
import json
import os
import re

from langchain_core.tools import tool

from agent.context import CATALOG, GENIE_COMMERCIAL, GENIE_SUPPLY, SCHEMA
from agent.mcp_client import genie_ask, sql_call
from memory.pgretry import retry_once

FQ = f"{CATALOG}.{SCHEMA}"
RE_MARKET = re.compile(r"^[A-Z]{2}$")
RE_DIST = re.compile(r"^[A-Z]{2}-D\d{1,2}$")
RE_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
RE_PROMO = re.compile(r"^PR-\d{3,5}$")
PROMO_STATUS = {"", "active", "planned", "completed"}
DOC_TYPES = {"promo_brief", "field_report", "policy", "distributor_agreement", "product_spec", "market_playbook"}
PROPOSAL_MAX_FACTOR = float(os.environ.get("BEV_PROPOSAL_MAX_FACTOR", "1.5"))
MAX_PROPOSALS_PER_TURN = int(os.environ.get("BEV_MAX_PROPOSALS_PER_TURN", "3"))
OOS_THRESHOLD_DAYS = float(os.environ.get("BEV_OOS_THRESHOLD_DAYS", "7"))  # policy: AT RISK below 7 days of cover
RE_SKU_CODE = re.compile(r"^[A-Z]{2}-[A-Z]{3,5}-\d{3,4}[A-Z]$")


def _resolve(conn, value, table, id_col, name_col, label):
    # exact name first, then a partial name that must match exactly one row
    rows = conn.execute(f"SELECT {id_col}, {name_col} FROM agent_ops.{table} WHERE lower({name_col}) = lower(%s)", (value,)).fetchall()
    if not rows:
        rows = conn.execute(f"SELECT {id_col}, {name_col} FROM agent_ops.{table} WHERE {name_col} ILIKE %s ORDER BY 1",
                            ("%" + value.replace("%", "") + "%",)).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        raise ValueError(f"unknown {label}: {value!r}")
    raise ValueError(f"ambiguous {label}, ask the user which one: " + "; ".join(f"{r[0]} {r[1]}" for r in rows[:6]))


def _sku_code(conn, sku):
    # accept SKU code or (partial) product name; memory rows and UC fns use the code
    v = (sku or "").strip()
    if not v or RE_SKU_CODE.match(v.upper()):
        return v.upper() or None
    return _resolve(conn, v, "ref_product", "sku", "product_name", "product")


def _dist_code(conn, dist):
    # accept distributor id or (partial) name
    v = (dist or "").strip()
    if RE_DIST.match(v.upper()):
        return v.upper()
    return _resolve(conn, v, "ref_distributor", "distributor_id", "distributor_name", "distributor")


def _soft(resolve, conn, value):
    # an ambiguous or unknown name becomes search text instead of failing the call
    try:
        return resolve(conn, value), ""
    except ValueError:
        return None, value


def _s(v, rx, label, allow_empty=False):
    v = (v or "").strip()
    if allow_empty and v == "":
        return "''"
    if not rx.match(v):
        raise ValueError(f"invalid {label}: {v!r}")
    return "'" + v.replace("'", "''") + "'"


PEOPLE_KEYS = ("requested_by", "decided_by", "user_id", "amended_by", "withdrawn_by")


def _people(obj, me):
    """Replace user emails in tool output with 'you' or a masked colleague id: tool outputs reach the LLM and the trace."""
    from agent.tracing import trace_user
    if isinstance(obj, list):
        return [_people(x, me) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in PEOPLE_KEYS and isinstance(v, str) and "@" in v:
                out[k] = "you" if me and v.lower() == me.lower() else f"a colleague ({trace_user(v)})"
            else:
                out[k] = _people(v, me)
        return out
    return obj


def _dump(obj):
    # Decimal (Postgres numeric) stays a number for the model and the approval card
    from decimal import Decimal
    return json.dumps(obj, default=lambda v: float(v) if isinstance(v, Decimal) else str(v))


def build_tools(ctx):
    # tools are built per turn (agent/bridge.py builds the graph per run): this counter is per turn (proposals + amends)
    proposals = {"n": 0}

    def dump(obj):
        return _dump(_people(obj, ctx.user_email))
    def _read(fn):
        # read-only Lakebase work: a dropped connection is retried once on a fresh one
        def run():
            with ctx.pg("planner") as conn:
                return fn(conn)
        return retry_once(run)

    def dist(v):
        return _read(lambda c: _dist_code(c, v))

    def sku_of(v):
        return _read(lambda c: _sku_code(c, v))

    @tool
    def ask_commercial(question: str) -> str:
        """Ask the Commercial & Promotions Genie agent (sell-out/depletions, sell-in/shipments, revenue, price, promotions). Use for open analytical questions; pass a complete, self-contained question."""
        return dump(genie_ask(ctx, GENIE_COMMERCIAL, question))

    @tool
    def ask_supply(question: str) -> str:
        """Ask the Supply & Replenishment Genie agent (inventory, stock cover, sell-in vs sell-out, replenishment). Use for open analytical questions not covered by the stock/OOS tools."""
        return dump(genie_ask(ctx, GENIE_SUPPLY, question))

    @tool
    def stock_cover(market: str, sku: str) -> str:
        """Days of stock cover per distributor for one SKU (code like VT-ORIG-473C or product name) in a market (2-letter code like TX; '' = all markets)."""
        return dump(sql_call(ctx, f"SELECT * FROM {FQ}.stock_cover({_s(market, RE_MARKET, 'market', True)}, {_s(sku_of(sku), RE_SKU_CODE, 'sku')})"))

    @tool
    def oos_risk_skus(market: str, threshold_days: float = 0) -> str:
        """Positions (distributor x SKU) at risk of running out: stock cover below threshold_days (0 = the policy's 7-day AT RISK line) in a market ('' = all markets), with revenue at risk."""
        t = float(threshold_days) or OOS_THRESHOLD_DAYS
        if not 0 < t <= 60:
            raise ValueError("threshold_days must be in (0, 60]")
        return dump(sql_call(ctx, f"SELECT * FROM {FQ}.oos_risk_skus({_s(market, RE_MARKET, 'market', True)}, {t})"))

    @tool
    def distributor_snapshot(distributor: str) -> str:
        """One-call health check of a distributor (id like NY-D2 or distributor name): stock positions below target cover, active promotions vs forecast, SKUs whose sell-out trend departs from the market, and a summary. Use it FIRST for "what needs attention at X", "how is X doing", or any distributor overview."""
        return dump(sql_call(ctx, f"SELECT * FROM {FQ}.distributor_snapshot({_s(dist(distributor), RE_DIST, 'distributor_id')})"))

    @tool
    def suggest_replenishment(distributor_id: str, sku: str) -> str:
        """Suggested replenishment quantity (cases) to bring a distributor x SKU position back to target cover. distributor_id like TX-D1 (a distributor name also works)."""
        return dump(sql_call(ctx, f"SELECT * FROM {FQ}.suggest_replenishment({_s(dist(distributor_id), RE_DIST, 'distributor_id')}, {_s(sku_of(sku), RE_SKU_CODE, 'sku')})"))

    @tool
    def promo_performance(market: str = "", status: str = "active") -> str:
        """Trade promotions with forecast vs measured uplift. market 2-letter code or ''; status active|planned|completed|''."""
        if status not in PROMO_STATUS:
            raise ValueError("status must be active, planned, completed or ''")
        return dump(sql_call(ctx, f"SELECT * FROM {FQ}.promo_performance({_s(market, RE_MARKET, 'market', True)}, '{status}')"))

    @tool
    def promo_by_distributor(promo_id: str) -> str:
        """Uplift of one promotion (like PR-0422) broken down by distributor, vs forecast."""
        return dump(sql_call(ctx, f"SELECT * FROM {FQ}.promo_by_distributor({_s(promo_id, RE_PROMO, 'promo_id')})"))

    @tool
    def search_documents(query: str, market: str = "", doc_type: str = "") -> str:
        """Hybrid search over company documents. Use to explain WHY (causes, rules, contract terms, approval rules and thresholds such as who approves N cases: these need no distributor or SKU). doc_type is optional: one or more of promo_brief, field_report, policy, distributor_agreement, product_spec, market_playbook, comma-separated (leave '' if unsure). For why a promotion or a distributor underperforms, use 'promo_brief,field_report': the cause is in the field report. Returns cited chunks (doc_id + section)."""
        from rag.search import doc_type_filter, search
        if market and not RE_MARKET.match(market):
            raise ValueError("invalid market")
        vec = ctx.embed([query])[0]
        types = doc_type_filter(doc_type, DOC_TYPES)
        hits = _read(lambda conn: search(conn, query, vec, market=market or None, doc_types=types, top_k=5))
        return dump([{k: h[k] for k in ("doc_id", "title", "section", "market", "content")} for h in hits])

    @tool
    def recall_decisions(query: str = "", market: str = "", sku: str = "", distributor_id: str = "") -> str:
        """Recall the team's past replenishment decisions (approved/rejected, who decided, with reasons): the 5 most relevant only, NOT a complete list. Call it FIRST for any question about replenishing, topping up or restocking a position, before recommending or proposing, and for 'why did we decide X'. Also call it, with the market, sku or distributor_id, for open-ended questions ('anything I should know', 'what's new', 'today', an update on a market, distributor or product) and report any active rejection or credit hold. Filter by market, sku or distributor_id when known. For counts or a summary of requests by status use requests_summary."""
        from memory.decisions import recall_decisions as recall
        def run(conn):
            s_, s_txt = _soft(_sku_code, conn, sku) if sku else (None, "")
            d_, d_txt = _soft(_dist_code, conn, distributor_id) if distributor_id else (None, "")
            # an ambiguous name (e.g. "Harbor Lager" = two SKUs) is matched as text, not as a SKU filter
            text = " ".join(t for t in (query, s_txt, d_txt) if t)
            return recall(conn, ctx.user_email, query=text or None, market_id=market or None, sku=s_ or None,
                          distributor_id=d_ or None, top_k=5, embed_fn=ctx.embed)
        rows = _read(run)
        return dump({"decisions": rows, "note": f"top {len(rows)} most relevant decisions, not a total; use requests_summary for counts"})

    @tool
    def requests_summary(scope: str = "mine", market: str = "", since_days: int = 0) -> str:
        """Complete counts of replenishment requests by status (proposed, approved, rejected, withdrawn) and distributor, with cases and items. scope = 'mine' (requested by this user) or 'team' (everyone). since_days 0 = all time. Use it for any summary, status overview or 'how many' question about requests; quote its totals exactly."""
        from memory.replenishment import summarize_requests
        if scope not in ("mine", "team"):
            raise ValueError("scope must be 'mine' or 'team'")
        m = (market or "").strip().upper() or None
        if m and not RE_MARKET.match(m):
            raise ValueError(f"invalid market: {market!r}")
        days = int(since_days or 0) or None
        out = _read(lambda c: summarize_requests(c, requested_by=ctx.user_email if scope == "mine" else None, market_id=m, since_days=days))
        return dump(out | {"scope": scope, "period": f"last {days} days" if days else "all time"})

    @tool
    def propose_replenishment(distributor_id: str, sku: str, qty_cases: float, reason: str) -> str:
        """Create a replenishment request (status proposed) for a distributor x SKU. It is NOT executed: an approver must approve or reject it. sku = SKU code or product name. qty_cases must not exceed 1.5x the suggest_replenishment quantity. At most 3 new requests per message; a repeat for the same distributor x SKU returns the open request (amend it instead). Always check recall_decisions first."""
        from memory.replenishment import propose_request
        if not ctx.can_propose:
            raise PermissionError("your role cannot propose replenishment requests")
        if proposals["n"] >= MAX_PROPOSALS_PER_TURN:
            raise ValueError(f"at most {MAX_PROPOSALS_PER_TURN} new requests per message: ask the user to confirm the rest in a new message")
        distributor_id, sku = dist(distributor_id), sku_of(sku)
        dist_sql = _s(distributor_id, RE_DIST, "distributor_id")
        # bound the proposal to the policy sizing (an injected doc or a model slip cannot inflate it)
        out = sql_call(ctx, f"SELECT recommended_qty_cases FROM {FQ}.suggest_replenishment({dist_sql}, {_s(sku, RE_SKU_CODE, 'sku')})")
        rec = next((float(r[0]) for r in (out.get("rows") or []) if r and r[0] is not None), 0.0)
        qty = float(qty_cases)
        if rec <= 0:
            raise ValueError("the replenishment policy recommends 0 cases for this position: nothing to propose")
        if qty > PROPOSAL_MAX_FACTOR * rec:
            raise ValueError(f"qty_cases {qty:g} exceeds {PROPOSAL_MAX_FACTOR:g}x the policy recommendation ({rec:g} cases)")
        with ctx.write() as conn:
            req = propose_request(conn, ctx.user_email, distributor_id, sku, qty, reason)
        if req.get("duplicate"):
            # nothing new was created: say so instead of announcing a second request
            return dump({"status": "already_open", "note": "an open request for this distributor and SKU already exists; amend it instead",
                          "request": req | {"recommended_qty_cases": rec}})
        proposals["n"] += 1
        return dump({"status": "awaiting_approval", "request": req | {"recommended_qty_cases": rec}})

    def _policy_qty(distributor_id, sku):
        out = sql_call(ctx, f"SELECT recommended_qty_cases FROM {FQ}.suggest_replenishment({_s(distributor_id, RE_DIST, 'distributor_id')}, {_s(sku, RE_SKU_CODE, 'sku')})")
        return next((float(r[0]) for r in (out.get("rows") or []) if r and r[0] is not None), 0.0)

    def _req_id(v):
        v = (v or "").strip()
        if not RE_UUID.match(v):
            raise ValueError("request_id must be the request's id (use my_open_requests to list them)")
        return v

    @tool
    def my_open_requests() -> str:
        """List this user's replenishment requests still waiting for a decision (status proposed), newest first, with their request_id."""
        from memory.replenishment import list_requests
        rows = _read(lambda c: list_requests(c, status="proposed", requested_by=ctx.user_email, limit=20))
        return dump(rows)

    @tool
    def amend_request(request_id: str, qty_cases: float = 0, reason: str = "") -> str:
        """Change the quantity and/or reason of one of this user's requests that is still proposed (nobody decided it yet). qty_cases 0 = keep it. The new quantity must not exceed 1.5x the policy recommendation."""
        from memory.replenishment import amend_request as amend, get_request
        if not ctx.can_propose:
            raise PermissionError("your role cannot change replenishment requests")
        if proposals["n"] >= MAX_PROPOSALS_PER_TURN:
            raise ValueError(f"at most {MAX_PROPOSALS_PER_TURN} request changes per message: ask the user to confirm the rest in a new message")
        rid = _req_id(request_id)
        qty = float(qty_cases) if qty_cases else None
        if qty is not None:
            row = _read(lambda c: get_request(c, rid))
            if not row:
                raise ValueError("request not found")
            rec = _policy_qty(row["distributor_id"], row["sku"])
            # fail closed like propose: no recommendation means no sizing bound to check against
            if rec <= 0:
                raise ValueError("the replenishment policy recommends 0 cases for this position: the quantity cannot be raised")
            if qty > PROPOSAL_MAX_FACTOR * rec:
                raise ValueError(f"qty_cases {qty:g} exceeds {PROPOSAL_MAX_FACTOR:g}x the policy recommendation ({rec:g} cases)")
        with ctx.write() as conn:
            res = amend(conn, rid, ctx.user_email, qty, reason or None)
        proposals["n"] += 1
        return dump({"status": "amended", "request": res})

    @tool
    def withdraw_request(request_id: str, reason: str) -> str:
        """Withdraw (cancel) one of this user's requests that is still proposed. Use it when the user no longer wants that replenishment."""
        from memory.replenishment import withdraw_request as withdraw
        rid = _req_id(request_id)
        with ctx.write() as conn:
            return dump({"status": "withdrawn", "request": withdraw(conn, rid, ctx.user_email, reason)})

    return [distributor_snapshot, ask_commercial, ask_supply, stock_cover, oos_risk_skus, suggest_replenishment, promo_performance,
            promo_by_distributor, search_documents, recall_decisions, requests_summary, propose_replenishment, my_open_requests,
            amend_request, withdraw_request]
