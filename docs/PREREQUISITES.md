# Prerequisites

Check every row before step 1 of [DEPLOYMENT.md](DEPLOYMENT.md). Each row names the role that usually holds it.

## Databricks: workspace features

| Requirement | Detail | Role |
|---|---|---|
| Workspace | Unity Catalog, serverless compute, Databricks Apps. Tested on Azure Databricks; the code uses no cloud-specific API. | Workspace admin |
| Apps user authorization | On-behalf-of user authorization enabled, and the workspace scope allowlist includes `sql`, `postgres`, `genie`, `ai-gateway`. | Workspace admin |
| Managed MCP servers (Preview) | Needed for the Genie space MCP endpoints the agent calls. | Workspace admin |
| Unity AI Gateway (Beta) | Model services for the LLMs and embeddings, and the `system.ai.*` services. | Workspace admin |
| Lakebase Autoscaling | Postgres projects with branches. **Lakebase Search** (`lakebase_vector` + `lakebase_text`) must be enabled on the project in its Settings page (UI only, not reversible). Without it, run the RAG steps with `--fallback` (pgvector HNSW + GIN). | Workspace admin |
| Genie | Genie spaces and the Genie API. | Workspace admin |
| MLflow | MLflow 3 tracing to Unity Catalog, Prompt Registry, `mlflow.genai` evaluation and production monitoring. | — |
| Tag policies | If the account enforces tag policies, set `BEV_TAGS` to allowed keys and values. | Account admin |

## Databricks: data, compute and models

| Resource | Detail | Role |
|---|---|---|
| Unity Catalog | An existing catalog where you have `USE CATALOG` + `CREATE SCHEMA`. The deployment creates `bev_ops_<env>` in it (tables, functions, metric views, Volume, model services, trace tables, prompts). | Catalog owner |
| Serverless SQL warehouse | An existing warehouse (`BEV_WAREHOUSE_ID`). The app runs SQL on it as the user, so every user needs `CAN_USE`. A dedicated warehouse keeps cost attribution clean. | Workspace admin |
| Foundation models | The Gateway model services point at `databricks-claude-haiku-4-5` (fast) and `databricks-claude-sonnet-5` (reasoning) by default; embeddings use `system.ai.gte-large-en` (1024 dims). Override with `BEV_LLM_FAST_MODEL` / `BEV_LLM_REASONING_MODEL` if those are not available in your region. | — |
| Lakebase | One Autoscaling project per environment (`bev-ops-<env>`), created by `deploy.sh <t> lakebase-project`. You become its owner; the setup steps run as owner. | You |
| Clusters, jobs, model serving endpoints | Not needed. | — |

## Account and identities

| Requirement | Detail | Role |
|---|---|---|
| Account admin | Create the planner, approver and viewer groups, assign them to the workspace, add members (`setup/identities.py`). | Account admin |
| Users | Planners read and propose; approvers also decide (four-eyes: never their own request); viewers read traces and the MLflow experiment. Each user needs one-time OAuth consent when they first open the app. | Account admin |
| App service principal | Created by the bundle deploy. The `app` phase grants it what it needs (Prompt Registry read, trace tables, Lakebase writes). | You |

## Your laptop

- Python 3.11 and `pip install -r requirements.txt` in a virtual environment.
- Node.js 20+ and npm, for the frontend build (`scripts/deploy.py build` runs `npm ci`).
- Databricks CLI with a workspace profile and an account profile (`databricks auth login`).
- Commands that grant permissions (`identities`, `access`, the `grants` step of `app`) are best run by you in your own terminal: some coding assistants block permission changes by design.
