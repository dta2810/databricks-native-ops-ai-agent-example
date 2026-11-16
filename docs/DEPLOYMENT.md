# Deployment guide

Step by step, in the order `deploy.sh` runs the phases. Each step says what gets created and why, the command that does it, and how to do it by hand. Where a phase supports it, run it first without `--apply` to see what it will do.

## 1. Prerequisites and laptop setup

Go through [PREREQUISITES.md](PREREQUISITES.md). Then:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
databricks auth login --host https://<workspace-host> --profile <workspace-profile>
databricks auth login --host https://accounts.<cloud-domain> --account-id <account-id> --profile <account-profile>
python -m pytest -q app/tests memory/test_proposal_limits.py agent/test_agent_routing.py rag/test_doc_type_filter.py eval tests
```

## 2. Configure the target

```bash
cp targets/example.env targets/env1.env
```

| Variable | Meaning |
|---|---|
| `BEV_ENV` | **Required.** Suffix for every resource name, 1-12 lowercase letters/digits. Groups and app names are account- or workspace-wide, so pick one nobody else uses. |
| `DATABRICKS_CONFIG_PROFILE`, `BEV_ACCOUNT_PROFILE` | CLI profiles for the workspace and the account. |
| `BEV_WORKSPACE_ID`, `BEV_CATALOG`, `BEV_WAREHOUSE_ID` | Where the data lives and which warehouse runs it. |
| `BEV_PLANNERS`, `BEV_APPROVERS`, `BEV_VIEWERS` | Group members (emails). Use at least two people for planner and approver, or four-eyes blocks every decision. |
| `BEV_TAGS` | Resource tags (JSON). |

Ids the scripts create (Genie spaces, MLflow experiment) are saved to `targets/env1.resolved.env`. From here on every command takes the target name (`env1`).

## 3. Data, functions and semantics in Unity Catalog

```bash
./deploy.sh env1 data --apply
```

1. `data/generate.py` writes the synthetic world locally (seeded): 8 US states, 3 distributors each, 18 SKUs in 5 brands, 78 weeks of depletions and shipments, inventory, promotions.
2. `setup/00_schema.sql` creates the schema `bev_ops_<env>` and a `landing` Volume; `setup/load_data.py` uploads and loads the tables.
3. `setup/20_semantics.sql`, `30`/`31_uc_docs*.sql` first (metric views, comments, keys and the helper views the functions read), then `setup/10_functions.sql`: governed functions the agent calls (stock cover, out-of-stock risk, replenishment suggestion, promotion performance, distributor snapshot).
4. `genie/supply_replenishment/layer1_supply.sql`: views for the supply Genie space.
5. `setup/11_test_functions.sql`: checks that the planted stories hold.

Manual alternative: run the SQL files in the order of `deploy.sh` in the SQL editor, replacing `${catalog}`, `${schema}` and `${volume}`.

## 4. Gateway model services

```bash
./deploy.sh env1 models --apply
```

`setup/model_services.py` creates `llm_fast`, `llm_reasoning` and `embeddings` as Unity AI Gateway model services in the schema, each pointing at one foundation model. The agent calls them by name, so swapping a model is a change to the service, not to the code. Manual alternative: Unity Gateway → Model services → Create, one per name.

## 5. Genie spaces

```bash
./deploy.sh env1 genie            # prints both space definitions
./deploy.sh env1 genie --apply
```

`genie/build_space.py` turns each YAML (`genie/commercial_promotions.yaml`, `genie/supply_replenishment/supply_replenishment.yaml`) into a Genie space: metric views, trusted functions, joins, SQL snippets, example questions and instructions, in that order of reliability. Titles end with `(<env>)`; the space ids are saved to the resolved file. `genie/run_benchmarks.py <yaml> <space_id>` replays the benchmark questions.

## 6. Lakebase

```bash
./deploy.sh env1 lakebase-project --apply
```

Creates the Autoscaling project `bev-ops-<env>` (Postgres 17, default branch and endpoint). Then, **in the UI**, open the project → Settings → enable **Lakebase Search**. It cannot be turned off later; without it, run the next phase's RAG steps with `--fallback`.

```bash
./deploy.sh env1 lakebase --apply
```

Run as the project owner. It creates, in order:

| Script | Schema | What |
|---|---|---|
| `40_lakebase_rag.py`, `41_load_corpus.py` | `rag` | Document chunks of `docs_corpus/`, embedded through the Gateway, with a vector index and a BM25 index (hybrid search, reciprocal rank fusion) |
| `50_lakebase_memory.py` (+ `--indexes`) | `agent_mem`, `agent_ops` | Decision log with its own search indexes; replenishment requests (proposed → approved, rejected, withdrawn); reference lookups from UC |
| `51_seed_memory.py` | `agent_mem` | Two seeded decisions through the real approval path |
| `53_lakebase_ui.py` | `agent_ui` | Chat threads and messages (every query filters by the token's user) |
| `54_lakebase_agent_state.py` | `agent_state` | LangGraph checkpoint tables, created by the owner so the app never runs DDL |

## 7. MLflow: experiment, trace location, prompts

```bash
./deploy.sh env1 mlflow --apply
```

1. `mlflow_setup/01_experiment.py` creates the experiment under your user folder and links its traces to Unity Catalog tables `<prefix>_otel_*` in the schema. It sets the experiment ACL: you `CAN_MANAGE`, the viewer group `CAN_READ`, no broad groups. The experiment id is saved to the resolved file.
2. `mlflow_setup/02_prompts.py` creates the prompt-only schema `<schema>_prompts` (override `BEV_PROMPT_SCHEMA`), registers the system and router prompt templates there and points the `production` alias at the latest version. The app loads prompts by alias, so a prompt change ships without a redeploy of code.

## 8. Account groups

```bash
./deploy.sh env1 identities            # dry run
./deploy.sh env1 identities --apply    # account admin
```

`setup/identities.py` creates `bev_ops_planner_<env>`, `bev_ops_approver_<env>` and `bev_ops_viewer_<env>` at account level, assigns them to the workspace and adds the members from the target file. It refuses to reuse a same-name group with other members (`--adopt` overrides).

Manual alternative: Account console → User management → Groups → Add group (×3) → Members; then Workspaces → your workspace → Permissions → add the three groups.

## 9. Lakebase access

```bash
./deploy.sh env1 access            # prints the SQL
./deploy.sh env1 access --apply
```

`setup/52_lakebase_access.py` creates one Postgres role per group (same name) and grants read access to the shared tables. The model: **groups read, the app service principal writes**. The app mints a group-scoped credential from the user's token for reads; every write (chats, proposals, decisions) goes through the app service principal after the app has checked the caller's group. Nothing is granted on `public`, and the app role cannot create objects.

## 10. The app

```bash
./deploy.sh env1 secrets --apply   # secret scope bev-ops-<env> + a random trace salt (never overwritten)
./deploy.sh env1 app            # dry run of every step
./deploy.sh env1 app --apply
```

`scripts/deploy.py all` runs:

1. **build**: frontend (`npm ci && npm run build`) and staging of the shared packages into `app/`;
2. **validate / deploy**: `databricks bundle validate` and `deploy` with every value from the target (`databricks.yml`, `resources/app.yml`); bundle state is kept per `BEV_ENV`;
3. **grants** (before the app starts, so the first prompt load already has access): UC privileges for the groups (functions, metric views, warehouse), Genie `CAN_RUN`, Prompt Registry access for the app service principal (EXECUTE, CREATE FUNCTION and MANAGE on the prompt schema only; loading fails with less, and MANAGE never touches the data schema), trace tables (viewer group read; the app service principal writes the four trace tables only), and Lakebase access again for the app role;
4. **app**: starts the app `bev-ops-<env>`;
5. **verify**: fails if the user authorization scopes did not survive the deploy, if the app is not running, or if the trace tables or Lakebase role have broader access than expected.

The app reads the salt through a secret resource and refuses to start without it while identity masking is on (`BEV_TRACE_MASK_USER`, default true). User identities in traces and the thread keys are HMAC values of the email with that salt; keep it secret and stable (`setup/trace_salt.py --rotate` replaces it and orphans existing threads).

Requests above the manager threshold (500 cases) need the manager group (`BEV_MANAGER_GROUP`; default: the approver group).

Write requests from the browser must be same-origin JSON (CSRF guard); other clients get 403.

Then open the app URL, accept the OAuth consent and send one message. Read `databricks apps logs bev-ops-<env>` after that first turn.

Four-eyes is on by default. For a single presenter on a personal sandbox, deploy with `BEV_DEPLOY_ALLOW_SELF_APPROVAL=true` **and** `BEV_DEPLOY_SINGLE_PRESENTER_MODE=1`; the UI then shows a "single-presenter mode" banner.

## 11. Online monitors

```bash
./deploy.sh env1 monitors            # dry run
./deploy.sh env1 monitors --apply
```

`scripts/online_monitoring.py` registers production monitors on the experiment: deterministic checks (approval guard, concise answers) and judges (leads with the answer, cites sources, session-level judges) that score a sample of live traces. Judges run on the `llm_fast` Gateway service. Evaluation traces are excluded.

## 12. Tags

```bash
./deploy.sh env1 tags            # check only
./deploy.sh env1 tags --apply
```

`setup/60_tags.py` tags the model services (billing), the Lakebase project, the schema and the experiment from `BEV_TAGS`. Request tags on every Gateway call (`surface`, `step`) are set by the agent itself.

## 13. Evaluate and smoke test

```bash
./deploy.sh env1 eval              # offline check of the dataset and scorers
./deploy.sh env1 eval --apply      # full run (several minutes)
./deploy.sh env1 smoke             # wakes every backend and asks each Genie space one question
```

`eval/run_eval.py` replays `eval/dataset.jsonl` through the agent in-process, one MLflow session per record, and scores it with deterministic checks and judges; results land in the experiment's evaluation runs. Eval writes to the same Lakebase memory as the app, so run it against its own `BEV_ENV` if real users share the environment. See [eval/README.md](../eval/README.md).

Try the questions in [EXAMPLE_USAGE.md](EXAMPLE_USAGE.md).
