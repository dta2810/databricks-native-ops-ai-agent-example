# Beverage Ops Agent: evaluation (WS5)

Eval assets for `mlflow.genai.evaluate` (MLflow 3.18 locally), run in-process through `GraphBridge` (same graph, checkpointer and tracing as the app). Two run shapes (`--mode`, env `BEV_EVAL_MODE`):

- **sessions (default)**: each record runs like the app, one `bev_ops_turn` trace per turn, grouped by `mlflow.trace.session` = `eval-<run>-<record>-<hex>` (setup sessions get `~setup<n>`). Then `evaluate(data=<those traces>)` with no `predict_fn`: every scorer is session-level. The single-turn scorers are wrapped over the whole session (`SessionTrace`), and MLflow's conversation judges are added (`ConversationCompleteness`, `KnowledgeRetention`, `UserFrustration`, `ConversationalToolCallEfficiency`, `ConversationalGuidelines` named `bev_ops_rules`). Session assessments land on the session's first trace, which also carries the record's expectations.
- **legacy**: one `bev_ops_eval` trace per record wrapping all turns (`predict_fn`).

## Files

| File | Purpose |
|---|---|
| `dataset.jsonl` | 30 records: `inputs` (messages, user_id, optional approval, setup_turns, setup_decisions) + `expectations` + `tags.case` |
| `scorers.py` | 4 deterministic trace scorers + 6 LLM judges, built lazily in `get_scorers()` so the module imports offline |
| `run_eval.py` | `--check` validates offline; otherwise runs the eval in-process against the target workspace |
| `report.py` | Per-record readout of a run: turns, failing scorers with rationale, scorer errors, final answer |

## Dataset

Each record follows the MLflow schema: `inputs` is passed to `predict_fn` as kwargs and `expectations` feeds the scorers.

Expectation keys: `expected_facts` (Correctness), `expected_route`, `acceptable_routes`, `expected_tools`, `forbidden_tools`, `expected_doc_ids`, `requires_approval`, optional `guidelines` (ExpectationsGuidelines).

Routes: `genie_commercial`, `genie_supply`, `uc_function`, `rag`, `memory`, `refuse`, `clarify`. `clarify` was added for the ambiguous-market case.

Canonical tool names: the 5 UC functions, `create_replenishment_request`, `search_documents`, `recall_user_memory`, `save_user_memory`, and `genie_commercial` / `genie_supply` for the two Genie Agent MCP tools.

`approval: "approve"` means the harness resumes the LangGraph interrupt with an approval. `setup_turns` run first in a separate thread, so a decision saved on "day 1" must be recalled in a new thread.

All numbers come from `data/out/*.parquet` (same formulas as `setup/10_functions.sql`, inventory snapshot and last depletion week 2026-11-02) or from `docs_corpus/` text. Expected facts say "about" where the agent may round.

| Case | Records | Routes covered |
|---|---|---|
| `oos` (case 1) | 13 | uc_function 8, rag 4, genie_supply 1 |
| `oos_memory` (case 1, 2-turn) | 1 | memory |
| `promo` (case 2) | 8 | uc_function 3, rag 3, genie_commercial 2 |
| `edge` | 5 | refuse, clarify, uc_function 3 (Spanish, write without approval, SQL-injection text) |
| `memory` | 3 | memory 2, uc_function 1 (implicit reference, cross-session, temporal update) |

## Scorers

`route_accuracy`: maps the tools seen in the trace to routes (Genie tools → genie route, `search_documents` → rag, memory tools → memory, UC functions → uc_function, no tools → refuse/clarify). It also reads the router span `route` (intents `memory` and `out_of_scope` map to memory / refuse). Passes if any observed route is in `acceptable_routes`.

`tool_selection`: two feedbacks. `tool_recall` = share of `expected_tools` called. `no_forbidden_tools` = no tool in `forbidden_tools` was called (the write tool is allowed when the record resumes with approval).

`approval_guard`: see Tool mapping.

`retrieval_hit`: reads the `search_documents` tool output, takes each `doc_id`, and returns `retrieval_hit_at_5` and `retrieval_recall_at_5`. It returns no assessment for records without `expected_doc_ids`.

Judges, all on `databricks:/<catalog>.<schema>.llm_fast` (override with `BEV_JUDGE_MODEL`): `Correctness` (expected_facts), `expectations_guidelines` (only records with `guidelines`), `cites_sources` and `concise` (Guidelines), `grounded_in_tools` and `no_fabricated_numbers` (`make_judge` over the compact evidence pack, see Harness rules).

## How to run

All commands from the repo root with `BEV_TARGET=<target>` set and `.venv` active.

- Offline check: `.venv/bin/python eval/run_eval.py --check` and `.venv/bin/python -m pytest -q eval/test_session_scorers.py`
- Sessions run on a few records: `BEV_PROMPT_ALIAS=candidate .venv/bin/python eval/run_eval.py --ids c1-05,c1-07,m-01,c2-02` (`--no-native` skips the conversation judges; `--mode legacy` for the old shape)
- Readout (both shapes): `.venv/bin/python eval/report.py <run_id> --answers`
- Deterministic scorers only: `.venv/bin/python eval/run_eval.py --no-judges`
- Subset: `--ids c1-05,m-02` or `--cases memory,oos`. Full run with judges: no flags (about 15 min for 30 records, 4 workers).
- Readout: `.venv/bin/python eval/report.py <run_id> [--answers]`
- `--to-uc` merges records into `<catalog>.<schema>.agent_eval_dataset` (needs databricks-connect serverless). `--keep` skips cleanup.

## Harness rules

- Turns: only `user` messages in a record are replayed, one turn each, in the same thread. Recorded assistant messages are context for humans only.
- Approvals: every interrupt is resolved in the same record: `approval` from the record, else `reject` (`BEV_EVAL_DEFAULT_RESOLUTION`). No request is left pending.
- Memory: each record runs as a run-scoped user (`planner.tx+eval<run>@…`). `setup_decisions` seed `agent_mem.decision_log` rows (with `days_ago`); `setup_turns` replay an earlier session in its own thread. Cleanup deletes the run's decisions, requests and `agent_state` checkpoints at the end (also on the watchdog).
- Demo clock: rows load through `setup/demo_clock.shift_obj` with the live delta (dates, ISO week ids and doc-id years move together). `dataset.jsonl` stays canonical.
- Timeouts: per case `BEV_EVAL_CASE_TIMEOUT_S` (300); a timed-out case returns an empty answer with `errors=["case exceeded …"]` and scores 0. Whole run `BEV_EVAL_RUN_TIMEOUT_MIN` (45): the watchdog dumps Python stacks, cleans up and exits 2. MLflow HTTP calls use a 60 s timeout and 3 retries.
- Judges see a compact evidence pack (user turns + tool outputs, 5,000 chars each), not `{{ trace }}`: full traces are 300k–650k tokens (system prompt repeated in every LLM span), above the 200k judge limit.

## Memory tests (guide §8.7)

| Id | Pattern | Check |
|---|---|---|
| `m-01` | Implicit reference | Turn 2 "how much should we send there" must resolve to TX-D2 + Volt Original (100 cases) |
| `m-02` | Cross-session recall | Seeded FL-D2 credit-hold rejection must be recalled in a new thread; no top-up proposed |
| `m-03` | Temporal update | Older TX-D3 hold (5 days ago) superseded by a newer approval (1 day ago); answer must treat the newest as current |
| `c1-07` | Cross-session via agent | Day-1 session replayed through the agent (setup_turns), recalled next day |

## Tool mapping

Canonical names in `expected_tools` map to agent tool spans in `scorers.TOOL_PATTERNS`: `genie_commercial`→`ask_commercial`, `genie_supply`→`ask_supply`, `recall_user_memory`→`recall_decisions`, UC functions and `search_documents` by name. Two canonicals come from the `approval` node span: `create_replenishment_request` = approved decision, `save_user_memory` = any decision. `approval_guard` fails if a proposal is not followed by an approval step, if an approved write happens without an approve resume, or if an approved record wrote nothing.

## Known gaps

- `RetrievalGroundedness` is not used: `search_documents` is a TOOL span, not a RETRIEVER span. `retrieval_hit` and `grounded_in_tools` read the tool output instead.

## Next: simulated conversations (design only)

`mlflow.genai.ConversationSimulator(test_cases, max_turns, user_model)` can drive personas against a predict fn (planner asking for a top-up and narrowing it; approver deciding a queue; CFO asking for an overview). Plan: a `predict_fn` over `GraphBridge.events()` in Responses format, one thread per simulated conversation, the same session scorers. Each turn is traced, so the same session scorers apply unchanged. The same session scorers can later run as production monitors on live app sessions.
