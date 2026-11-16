# OBO scopes for bev-ops-agent

Researched 2026-11-10 against docs.databricks.com. "Verified" = stated in current docs; "Unverified" = inferred, confirm after the first deploy.

## Result

Declare `user_api_scopes: [sql, postgres, genie, ai-gateway]` (current set). It is sufficient for every user-identity call the app makes. MLflow tracing and Prompt Registry are NOT available as an Apps user scope, so they run as the app service principal.

## Per call path

| Call | Scope / identity | Status | Source |
|---|---|---|---|
| Genie Agent MCP `/api/2.0/mcp/genie/{space_id}` | `genie` (user also needs CAN RUN on the space + its tables/warehouse). Calls bypass Unity Gateway. Workspace admin must enable Managed MCP Servers (Public Preview). | Verified | [Databricks-provided MCPs → Endpoints not governed by Unity Gateway](https://docs.databricks.com/aws/en/agents/mcp-tools/built-in-mcp-services) |
| Gateway MCP service `/ai-gateway/mcp-services/system.ai.dbsql` | `ai-gateway`; caller needs EXECUTE on the service + USE CATALOG/SCHEMA (default for account users on `system.ai.*`) + warehouse/UC permissions. First call per user may return JSON-RPC `-32042` with a login URL. Requires the Unity Gateway beta. | Verified | [Authentication for agents → Authenticate to MCP Services](https://docs.databricks.com/aws/en/agents/custom-agents/agent-authentication); built-in MCPs page |
| Gateway model services `/ai-gateway/mlflow/v1/chat/completions` (`llm_fast`, `llm_reasoning`, embeddings) | `ai-gateway` (same Gateway token forwarding as MCP services); user needs EXECUTE on the model service. | Unverified for model services specifically (docs state it for MCP services; same Gateway surface). Check with one deployed request. | agent-authentication page; API scopes page lists Gateway APIs under `unity-catalog` for management, not for invocation |
| Lakebase credential minting (`postgres.generate_database_credential(group_name=)`) | `postgres` (scope includes "Generate Database Credential" for Postgres); app also needs the `postgres` resource CAN_CONNECT_AND_CREATE. | Verified | [Configure authorization → Supported scopes](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth); [API scopes → Postgres Database](https://docs.databricks.com/api/workspace/scopes) |
| Warehouse statement execution (overview/exec/distributor endpoints, `server/warehouse.py`) | `sql` | Verified | Configure authorization page |
| MLflow tracing to the agent experiment (UC trace location) | App service principal: experiment resource `CAN_EDIT`. The `mlflow` OAuth scope exists for tokens but is NOT in the Databricks Apps supported user scopes, so OBO is not possible. For UC-backed traces the SP likely also needs SELECT/MODIFY on `<catalog>.<schema>.mlflow_experiment_trace_*` and CAN USE on the trace warehouse. | Experiment part verified; UC trace-table grants unverified | agent-authentication → Grant permissions to the MLflow experiment; supported scopes list |
| Prompt Registry load (`prompts:/<catalog>.<prompt_schema>.bev_ops_system@production`) | App service principal: CREATE FUNCTION, EXECUTE and MANAGE on the dedicated prompt schema (`<schema>_prompts`, no tables). EXECUTE + USE SCHEMA alone returned PERMISSION_DENIED live (2026-11-16), so the full set is needed; it is never granted on the data schema. | Verified live | agent-authentication → Grant permissions to other Databricks resources |

## Notes

- Current scope names: `genie` (not `dashboards.genie`), `model-serving` (not `serving.*`), `sql` (replaces all `sql.*`). `model-serving` is not needed: no serving endpoints are called directly.
- Optional tightening: `sql:restricted-query` (read-only SQL) would fit the dashboard queries, but the dbsql MCP path goes through `ai-gateway`, so keep `sql` until writes are confirmed absent.
- Verify after deploy: one chat turn → `system.query.history` shows the user (not the SP) for Genie/UC function SQL; Lakebase rows carry the user email; Gateway usage rows (`system.ai_gateway.usage`) attribute to the user. If the SP UUID shows up, the forwarded token is not reaching that call.
- Prompt load and tracing must use the SP client (`WorkspaceClient()` / default MLflow auth in the app), never the user token.
- Admin prerequisites on the workspace: user authorization enabled, Managed MCP Servers preview on, Unity Gateway beta on, workspace scope allowlist (`allowedAppsUserApiScopes`) includes all four scopes.
