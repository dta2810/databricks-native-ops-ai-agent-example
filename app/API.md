# Beverage Ops Agent: app API

All routes are under `/api`. Every call runs as the signed-in user: the Apps proxy forwards the user's OAuth token (`x-forwarded-access-token`), UC queries run on the SQL warehouse with that token, and Lakebase connections use a credential minted for the user's group (`bev_planner` or `bev_approver`). Per-user isolation in Lakebase is enforced in SQL (`user_id` = token identity), because group roles make `current_user` the group. Errors return `{"detail": "<generic message>"}`; hosts, tokens and SQL never reach the client.

Status codes: `400` invalid input, `401` no or invalid token, `403` missing group or UC permission, `404` not found or not yours, `409` nothing to resume, `422` body validation, `502` data or model service failed.

## Identity and configuration

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | `{ok: true}` |
| GET | `/me` | `{email, roles: [{role, label, can_propose, can_approve}], can_approve, agent_mode, local_dev}`. Roles = groups the platform lets the user mint a Lakebase credential for. |
| GET | `/demo-cases` | `{company, product, currency, stories: [{key, title, prompts[]}], ask: {template_key: "text with {{vars}}"}}` from `config/demo_cases.yaml` (`BEV_DEMO_CASES`) and `server/prompts/ask_templates.yaml`. Reloaded when the file changes. |
| GET | `/markets` | `{markets: [{market_id, market_name, region}], distributors: [{distributor_id, distributor_name, market_id, channel}]}` from `dim_market` / `dim_distributor` (cached 10 min per user). |

## Conversations (Lakebase `agent_ui`)

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/threads` | `q` (optional title search) | `{threads: [{thread_id, title, created_at, updated_at, pending_approval}]}`, current user only, newest first |
| POST | `/threads` | `{title?}` | the new thread (title defaults to `New conversation`, replaced by the first user message) |
| GET | `/threads/{thread_id}/messages` | | `{thread, messages: [{id, role, content, status, error, tool_events[], sources[], approval, trace_id, feedback, created_at}]}`; `404` if not the caller's |
| PATCH | `/threads/{thread_id}` | `{title}` | `{ok}` |
| DELETE | `/threads/{thread_id}` | | `{ok}` (soft delete: `deleted_at`) |

`thread_id` is a UUID. The agent-side key passed to the bridge (and the LangGraph checkpointer) is `<sha256(email)[:12]>-<thread_id>`.

## Chat turns (Server-Sent Events over POST)

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/chat` | `{thread_id, message}` (1–4000 chars) | Needs a group. Creates the thread if the id is new; `404` if it belongs to someone else. Persists the user message, streams the turn, persists the folded assistant turn when the stream ends (also on error or disconnect). |
| POST | `/chat/feedback` | `{thread_id, trace_id, rating: up\|down, comment?}` (comment ≤ 500) | Own thread + own assistant message with that `trace_id`, else `404`. Saves `feedback` on the message and logs `mlflow.log_feedback(name=user_feedback, value=bool, source=HUMAN/<email>)` on the trace (as the app SP). `409` in stub mode (no traces). `502` if the trace write fails (feedback already saved). |
| POST | `/chat/resume` | `{thread_id, decision: approve\|reject, reason?}` (reason ≤ 500) | Needs `bev_approver`. `409` when nothing is pending. If the process restarted, the stub re-arms from the persisted approval card; the graph resumes from its checkpointer. The `decision` event also updates the stored card. |

Response: `text/event-stream`, one `data: {json}` per event.

| `type` | Fields | UI |
|---|---|---|
| `token` | `text` | appended to the answer |
| `tool_start` | `id, tool, kind (genie\|uc_function\|documents\|memory\|request), label, input` | running chip |
| `tool_end` | `id, ok, summary, sources[], rows[]?` | chip result; click to expand |
| `approval_required` | `request {market_id, distributor_id, distributor_name, sku, product_name, qty_cases, reason, cover_days, target_cover_days, needs_manager_approval}` | approval card |
| `decision` | `status (approved\|rejected), request, decided_by` | updates the card |
| `final` | `trace_id?` (graph mode: the turn's MLflow trace, `trace:/<cat>.<sch>.<prefix>/<id>`) | end of turn; shows 👍/👎 |
| `error` | `message` | inline error |

`sources[]` items: `{kind: "doc", doc_id, section, snippet}`, `{kind: "sql", title, sql}`, `{kind: "memory", decision_id, created_at, summary}`.

### Bridge contract (`server/agent_bridge.py`)

```python
class AgentBridge(Protocol):
    mode: str
    def run(self, thread_id: str, message: str, user: User) -> AsyncIterator[dict]: ...
    def resume(self, thread_id: str, decision: str, reason: str | None, user: User) -> AsyncIterator[dict]: ...
    def pending_approval(self, thread_id: str) -> dict | None: ...
```

`BEV_AGENT_MODE=graph` loads `agent.bridge.GraphBridge`; `stub` (default) is a scripted preview that calls the real tools as the user (UC functions, Lakebase hybrid search, decision recall) but writes nothing. `User` gives `email`, `roles`, `can_approve`, `client()` (user-scoped `WorkspaceClient`) and `connection(role)` (OBO Lakebase pool).

## Executive overview

| Method | Path | Query | Source |
|---|---|---|---|
| GET | `/exec/kpis` | | `{as_of_week, tiles: [{id, label, value, format, delta, delta_label, subtitle, tone}]}`: net revenue and depletions last 4 weeks (`depletions_metrics` MEASURE; vs prior 4 weeks / same weeks last year), positions at OOS risk and weekly revenue at risk (`oos_risk_skus(NULL, BEV_OOS_THRESHOLD_DAYS)`), in-flight promotions (`promo_performance(NULL, 'Active')`), requests waiting (`agent_ops.replenishment_requests`). Cached 60 s per user. |
| GET | `/exec/trend` | `market?`, `weeks` (4–78, default 26) | `{rows: [{week_start, sell_in_cases, sell_out_cases}]}` from `v_sell_in_out_weekly` |
| GET | `/exec/alerts` | | `{alerts: [{id, category (oos\|promo\|request), severity, title, body, market, ask}]}` from `oos_risk_skus`, `v_promo_distributor_uplift` (active, gap < −20 pts) and pending requests |
| GET | `/exec/insights` | `refresh=true` to regenerate | `{bullets[3], model, as_of_week, generated_at, latency_ms, cached}`: `llm_fast` (Gateway `chat/completions`, user token, `max_tokens` = `BEV_INSIGHTS_MAX_TOKENS`) over the KPI + alert JSON only; system prompt `server/prompts/insights.md` with `cache_control`; cached per (user, page, snapshot hash) for `BEV_INSIGHTS_TTL_S` |
| GET | `/overview/oos` | `market?`, `threshold_days` (0–60) | `{threshold_days, rows}` from `oos_risk_skus` |
| GET | `/overview/promo` | `promo_id?` (default: active promo with the largest shortfall) | `{promo_id, selected, active[], distributors[]}` from `promo_performance` + `promo_by_distributor` |

## Market / distributor 360

| Method | Path | Query | Source |
|---|---|---|---|
| GET | `/distributors/{id}/summary` | | `{distributor, cover[] (v_stock_cover), promos[] (v_promo_distributor_uplift, active), counts {at_risk, below_target, skus}}` |
| GET | `/distributors/{id}/trend` | `sku?`, `weeks` | `{rows}` from `v_sell_in_out_weekly` |
| GET | `/distributors/{id}/activity` | | `{requests[] (agent_ops), decisions[] (agent_mem, the caller's own)}` |

## Requests

| Method | Path | Query | Returns |
|---|---|---|---|
| GET | `/requests` | `status?` (proposed\|approved\|rejected), `market?`, `limit` (≤ 100) | `{rows}` via `memory/replenishment.list_requests` |
