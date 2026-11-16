# Operations

Running the example day to day, tearing it down, known gotchas and the security checklist.

## Before a session: data clock and warm-up

The synthetic world has one canonical latest week. Shift it so the latest data week is the last completed week (numbers never change, only dates; UC tables, Lakebase rows and the corpus move together):

```bash
BEV_TARGET=env1 python scripts/refresh_dates.py --dry-run
BEV_TARGET=env1 python scripts/refresh_dates.py
./deploy.sh env1 smoke          # wakes Lakebase, the warehouse, both Genie spaces, the models; exits 1 on any failure
```

`BEV_TARGET=env1 python scripts/trace_health.py --hours 24` lists turns that ended in error or never finished.

## Teardown

```bash
./teardown.sh env1            # dry run: lists what it would delete
./teardown.sh env1 --apply
```

Order:
1. destroy the app bundle;
2. delete the MLflow experiment (its monitors go with it);
3. trash both Genie spaces;
4. drop the schema (tables, functions, metric views, Volume, trace tables, prompts);
5. delete the Lakebase project and the secret scope;
6. delete the account groups (only if every member is one of this environment's configured users).

Check Unity Gateway → Model services for any service left in the schema.

## Gotchas

| Symptom | Cause / fix |
|---|---|
| `verify` fails with missing scopes | A bundle deploy replaces the app record. Keep `user_api_scopes` in `resources/app.yml` identical to `app/app.yaml`, and re-run `./deploy.sh <t> app --apply`. |
| 403 on the first message | The user has not accepted the OAuth consent, is in no group, or lacks `CAN_USE` on the warehouse. |
| RAG step fails with `shared_preload_libraries` | Lakebase Search is not enabled on the project. Enable it in Settings, or use `--fallback`. |
| BM25 results look inverted | The BM25 operator sorts ascending; keep `ORDER BY ... ASC` in `rag/search.py`. |
| Decisions are always blocked | Planner and approver are the same person (four-eyes). Use two people, or single-presenter mode on a personal sandbox. |
| Genie step updates the wrong space | Space ids come from the resolved file; delete the stale `BEV_GENIE_*` line and re-run. Titles end with `(<env>)`. |
| `Too many messages` | Per-user chat limit (`BEV_CHAT_RATE_PER_MIN`, default 10 per minute). |
| App fails to start: trace salt missing | Masking is on and no secret salt is configured: run `./deploy.sh <t> secrets --apply`, then redeploy. |
| 403 on POST from a script | The CSRF guard needs `Content-Type: application/json` and a same-origin browser request; call the API from the app UI. |
| Prompt changes do not show | The app loads the `production` alias; move the alias after `02_prompts.py`. |

## Security checklist (before you share a deployment)

- [ ] No personal access tokens. Users act through Apps user authorization; MLflow and prompt loading use the app service principal.
- [ ] Four-eyes is on (`BEV_DEPLOY_ALLOW_SELF_APPROVAL=false`), or single-presenter mode is visible and limited to a personal sandbox.
- [ ] Groups hold only what `deploy.py grants` gives them; `verify` passed (scopes, trace-table access, Lakebase role without `CREATE`).
- [ ] The MLflow experiment is readable only by the viewer group. Traces hold questions and data rows; user identities are masked with a secret salt.
- [ ] Proposal caps, chat rate limit and the one-turn-per-thread rule are at their defaults or tighter.
- [ ] Eval runs against their own `BEV_ENV` when real users share the environment.
- [ ] `targets/*.env` and `*.resolved.env` stay out of version control (gitignored).
