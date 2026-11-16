# Databricks-native operations AI agent

An operations agent for a beverage distributor, built end to end on Databricks. It answers supply and commercial questions from governed data, remembers past decisions, proposes replenishment orders, and waits for a human to approve them. Every turn is traced, evaluated and monitored with MLflow. The company, data and documents are fictitious (Acme Beverages, synthetic data).

> Example code, not a product. Some features it uses are Preview or Beta and can change.

## Why

Most agent prototypes stitch together a vector store, a chat framework, a model API, a database for memory and a separate tool for evaluation. Each seam has its own identity, permissions and logs, and the quality loop lives outside the data platform. This example keeps every piece on Databricks:

1. **Governed data and semantics as tools.** The agent reads through Unity Catalog functions, metric views and two Genie spaces, as the signed-in user. Row-level and column permissions apply to the agent the same way they apply to a dashboard.
2. **Memory and actions in Lakebase.** Decision memory, hybrid document search (vector + BM25), the request queue and the chat threads live in Postgres next to the lakehouse. Writes go through a human-in-the-loop approval with four-eyes by default.
3. **A quality loop that closes inside the platform.** MLflow traces every turn to Unity Catalog, prompts are versioned in the Prompt Registry, offline evaluation and online monitors score the same traces, and a prompt change ships by moving an alias.
4. **One app, one identity model.** A Databricks App serves the UI and the agent with on-behalf-of user authorization; LLM calls go through Unity AI Gateway model services with usage tags.

## Architecture

```
 user ──► Databricks App (React UI + FastAPI, on-behalf-of the user)
              │
              └─► LangGraph agent ── Unity AI Gateway model services (fast / reasoning LLM, embeddings)
                     │
                     ├─ Unity Catalog: governed functions, metric views (SQL warehouse, as the user)
                     ├─ Genie spaces ×2 (Commercial & Promotions, Supply & Replenishment) via MCP
                     ├─ Lakebase (Postgres): decision memory · hybrid RAG · requests + approvals · threads · checkpoints
                     └─ human-in-the-loop approval card (four-eyes)
              │
              └─► MLflow: traces in UC tables · Prompt Registry · offline eval · online monitors
```

| Layer | Component | What it controls |
|---|---|---|
| Experience | Databricks App (FastAPI + React) | Chat with saved threads, executive overview, distributor 360, request queue |
| Identity | Apps user authorization (`sql`, `postgres`, `genie`, `ai-gateway`) | Data calls run as the signed-in user; MLflow and prompt loading run as the app service principal |
| Reasoning | LangGraph agent + Gateway model services | Routing, tool use, answer drafting; request tags on every call |
| Data and semantics | UC functions, metric views, Genie spaces | Governed numbers; the same permissions as any other consumer |
| Memory and actions | Lakebase: `agent_mem`, `rag`, `agent_ops`, `agent_ui`, `agent_state` | Decisions, documents, proposals and approvals, threads; group roles read, the app writes after checking the caller's group |
| Quality | MLflow tracing, Prompt Registry, `eval/`, online monitors | Every turn traced to UC; prompts by alias; scorers offline and on live traffic |

## How to start

```bash
git clone https://github.com/dta2810/databricks-native-ops-ai-agent-example && cd databricks-native-ops-ai-agent-example
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp targets/example.env targets/env1.env        # fill in workspace, catalog, warehouse, BEV_ENV, group members
./deploy.sh env1 genie                         # dry run: prints the Genie space definition it would deploy
```

Then follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) phase by phase (`./deploy.sh env1 <phase> --apply`). Every resource name ends with your `BEV_ENV` suffix, so several environments can share one workspace.

## Docs

| Document | Read it to |
|---|---|
| [docs/PREREQUISITES.md](docs/PREREQUISITES.md) | Check workspace features, compute, models and account permissions, and who must grant each |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Deploy step by step: what each phase creates, why, and how to do it by hand |
| [docs/EXAMPLE_USAGE.md](docs/EXAMPLE_USAGE.md) | Try it: example questions, the approval flow, and where to see traces, evals and monitors |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Refresh the data clock, warm up, tear down, fix known gotchas, run the security checklist |

## Repository layout

| Path | Contents |
|---|---|
| `deploy.sh`, `teardown.sh` | Entry points per phase (dry run unless `--apply` where supported) |
| `targets/` | Target template; your copies and generated ids are gitignored |
| `data/`, `docs_corpus/` | Synthetic data generator and the document corpus for search |
| `setup/` | UC SQL, Lakebase schemas and access, model services, tags, account groups |
| `genie/` | Genie spaces as config (YAML) + builder and benchmarks |
| `agent/`, `memory/`, `rag/` | LangGraph agent, tools, decision memory, requests, hybrid retrieval |
| `app/` | Databricks App: FastAPI server and React frontend |
| `mlflow_setup/`, `eval/`, `scripts/online_monitoring.py` | Experiment + UC trace location, Prompt Registry, offline eval, online monitors |
| `databricks.yml`, `resources/` | Bundle for the app (`scripts/deploy.py` passes every value from the target) |
