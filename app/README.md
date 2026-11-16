# Beverage Ops Agent app

FastAPI + React 18 (Tailwind, recharts) Databricks App for the Beverage Ops use case. Pages: Assistant (chat with persisted conversations, tool chips, sources, approval card), Executive overview (KPIs, AI insights, alerts, sell-in vs sell-out, promo tracker, OOS table), Markets (distributor 360), Requests. API contract: [API.md](API.md).

## Run locally

```bash
python3 -m venv .venv-app && .venv-app/bin/pip install -r app/requirements.txt pytest httpx   # from the repo root
(cd app/frontend && npm ci && npm run build)
cd app && LOCAL_DEV=true ../.venv-app/bin/python -m uvicorn app:app --port 8765
```

`LOCAL_DEV=true` reads `targets/<BEV_TARGET>.env`, uses the CLI profile as the user and connects to Lakebase with your own credential (no group roles). It refuses to start inside Databricks Apps. Tests: `cd app && ../.venv-app/bin/python -m pytest -q tests`.

## Configuration

Every id comes from env (bundle vars in Apps, `targets/<BEV_TARGET>.env` locally): `BEV_CATALOG`, `BEV_SCHEMA`, `BEV_WAREHOUSE_ID`, `BEV_LAKEBASE_PROJECT|BRANCH|ENDPOINT|DATABASE`, `BEV_PLANNER_GROUP`, `BEV_APPROVER_GROUP`, `BEV_LLM_FAST_SERVICE`, `BEV_EMBEDDING_SERVICE`, `BEV_GENIE_COMMERCIAL`, `BEV_GENIE_SUPPLY`, `BEV_AGENT_MODE`, `BEV_DEMO_CASES`, `BEV_OOS_THRESHOLD_DAYS`, `BEV_INSIGHTS_TTL_S`, `BEV_INSIGHTS_MAX_TOKENS`, `BEV_TURN_TIMEOUT_S` (default 240: a turn with no progress for that long gets an error and its trace is closed as ERROR). Demo prompts live in `config/demo_cases.yaml`; prompt text in `server/prompts/`.

## Demo prep and health

- `python3 scripts/warmup.py [--ask-genie]`: wakes Lakebase, the warehouse, both Genie spaces, the Gateway models and embeddings; prints the time per component, exits 1 on any failure. Run it after `scripts/refresh_dates.py` and a few minutes before the demo.
- `GET /api/health`: `turns_in_flight`, `turns_stuck` (older than `BEV_TURN_TIMEOUT_S`), `oldest_turn_s`.
- `python3 scripts/trace_health.py [--hours 24] [--stuck-min 10]`: `bev_ops_turn` traces by state; lists ERROR and stuck IN_PROGRESS ones, exits 1 if any.

## Deploy

Deploy with the repo's phases (`./deploy.sh <target> app --apply`), after the data, Genie, Lakebase, MLflow, groups, access and secrets phases. See [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md) step 10. `build` stages `config/ memory/ rag/ agent/ mlflow_setup/` into `app/` (gitignored). `verify` fails if the user authorization scopes (`sql postgres genie ai-gateway`) did not survive the deploy, if the trace tables or the Lakebase app role have broader access than expected, or if the app is not running. Each user needs: membership in a group, `CAN_USE` on the warehouse, and one-time OAuth consent; the `grants` step gives the groups the rest.
