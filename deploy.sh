#!/usr/bin/env bash
# One entry point per phase. Phases run in the order below; docs/DEPLOYMENT.md explains each one and the manual steps.
# Usage: ./deploy.sh <target> <phase> [--apply]
#   Without --apply, phases that support it print what they would do; the others stop and ask for --apply.
set -euo pipefail
cd "$(dirname "$0")"

usage() {
  cat <<'USAGE'
usage: ./deploy.sh <target> <phase> [--apply]
phases (in order):
  data        synthetic data, UC schema + tables, functions, metric views, UC docs, SQL tests      (DEPLOYMENT.md step 3)
  models      Unity AI Gateway model services (fast, reasoning)                                    (DEPLOYMENT.md step 4)
  genie       two Genie spaces (Commercial & Promotions, Supply & Replenishment)                   (DEPLOYMENT.md step 5)
  lakebase-project  Lakebase Autoscaling project (then enable Lakebase Search in the UI)          (DEPLOYMENT.md step 6)
  lakebase    Lakebase schemas: RAG corpus, decision memory, requests, chat threads, agent state   (DEPLOYMENT.md step 6)
  mlflow      MLflow experiment linked to UC traces, prompts in the UC Prompt Registry             (DEPLOYMENT.md step 7)
  identities  account groups (planner, approver, viewer), workspace assignment, members            (DEPLOYMENT.md step 8)
  access      Lakebase group roles + grants                                                        (DEPLOYMENT.md step 9)
  secrets     secret scope + random trace salt (user identities in traces are HMAC-masked)       (DEPLOYMENT.md step 10)
  app         build, bundle deploy, start the Databricks App, UC/Genie/Prompt grants, verify       (DEPLOYMENT.md step 10)
  monitors    MLflow production monitors (online scorers)                                          (DEPLOYMENT.md step 11)
  tags        resource tags (model services, Lakebase project, schema, experiment)                 (DEPLOYMENT.md step 12)
  eval        offline evaluation run on the eval dataset                                           (DEPLOYMENT.md step 13)
  smoke       warm up every backend and ask each Genie space one question                          (DEPLOYMENT.md step 13)
USAGE
  exit 1
}

[[ $# -lt 2 ]] && usage
export BEV_TARGET="$1"; PHASE="$2"; shift 2
APPLY=""
for a in "$@"; do
  case "$a" in
    --apply) APPLY="--apply" ;;
    *) usage ;;
  esac
done
[[ -f "targets/$BEV_TARGET.env" ]] || { echo "missing targets/$BEV_TARGET.env (copy targets/example.env)"; exit 1; }
PY="${PYTHON:-python3}"

need_apply() { [[ -n "$APPLY" ]] || { echo "dry run: '$PHASE' calls workspace APIs directly; re-run with --apply"; exit 0; }; }

case "$PHASE" in
  data)
    need_apply
    "$PY" data/generate.py
    "$PY" setup/sqlrun.py setup/00_schema.sql
    "$PY" setup/load_data.py
    # views first (30 creates v_stock_cover / v_promo_distributor_uplift that the functions read), then the functions
    "$PY" setup/sqlrun.py setup/20_semantics.sql setup/30_uc_docs.sql setup/31_uc_docs_v2.sql setup/10_functions.sql \
      genie/supply_replenishment/layer1_supply.sql
    "$PY" setup/sqlrun.py setup/11_test_functions.sql ;;
  models)
    need_apply
    "$PY" setup/model_services.py ;;
  genie)
    if [[ -n "$APPLY" ]]; then
      "$PY" genie/build_space.py genie/commercial_promotions.yaml --validate-sql
      "$PY" genie/build_space.py genie/supply_replenishment/supply_replenishment.yaml --validate-sql
    else
      "$PY" genie/build_space.py genie/commercial_promotions.yaml --dry-run
      "$PY" genie/build_space.py genie/supply_replenishment/supply_replenishment.yaml --dry-run
    fi ;;
  lakebase-project)
    "$PY" setup/lakebase_project.py $APPLY ;;
  lakebase)
    need_apply
    "$PY" setup/lakebase_conn.py probe
    "$PY" setup/40_lakebase_rag.py
    "$PY" setup/41_load_corpus.py
    "$PY" setup/50_lakebase_memory.py
    "$PY" setup/50_lakebase_memory.py --indexes
    "$PY" setup/51_seed_memory.py
    "$PY" setup/53_lakebase_ui.py
    "$PY" setup/54_lakebase_agent_state.py ;;
  mlflow)
    need_apply
    "$PY" mlflow_setup/01_experiment.py
    "$PY" mlflow_setup/02_prompts.py ;;
  identities)
    "$PY" setup/identities.py $APPLY ;;
  access)
    "$PY" setup/52_lakebase_access.py $APPLY ;;
  secrets)
    "$PY" setup/trace_salt.py $APPLY ;;
  app)
    if [[ -n "$APPLY" ]]; then "$PY" scripts/deploy.py all; else "$PY" scripts/deploy.py all --dry-run; fi ;;
  monitors)
    if [[ -n "$APPLY" ]]; then "$PY" scripts/online_monitoring.py; else "$PY" scripts/online_monitoring.py --dry-run; fi ;;
  tags)
    if [[ -n "$APPLY" ]]; then "$PY" setup/60_tags.py; else "$PY" setup/60_tags.py --check; fi ;;
  eval)
    if [[ -n "$APPLY" ]]; then "$PY" eval/run_eval.py; else "$PY" eval/run_eval.py --check; fi ;;
  smoke)
    "$PY" scripts/warmup.py --ask-genie ;;
  *) usage ;;
esac
