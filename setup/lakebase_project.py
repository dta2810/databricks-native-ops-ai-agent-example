"""Create the Lakebase Autoscaling project for this environment if it is missing (idempotent).

Lakebase Search (lakebase_vector + lakebase_text, used by the hybrid RAG) can only be enabled in the project's
Settings page in the UI, and the change cannot be undone: do it right after this step (docs/DEPLOYMENT.md step 6).
Without it, run the RAG steps with --fallback (pgvector HNSW + GIN).
Usage: BEV_TARGET=<target> python setup/lakebase_project.py [--apply]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    apply = ap.parse_args().apply
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.postgres import Project, ProjectSpec
    w = WorkspaceClient(profile=C.PROFILE)
    name = f"projects/{C.LAKEBASE_PROJECT}"
    existing = next((p for p in w.postgres.list_projects() if p.name == name), None)
    if existing:
        print(f"{name}: exists ({existing.uid})")
        return
    print(f"-> create Lakebase project {C.LAKEBASE_PROJECT} (Postgres 17, default branch + endpoint)")
    if not apply:
        print("dry run: add --apply to create")
        return
    op = w.postgres.create_project(project=Project(spec=ProjectSpec(display_name=f"Beverage Ops Agent ({C.ENV})", pg_version=17)),
                                   project_id=C.LAKEBASE_PROJECT)
    p = op.wait()
    print(f"{name}: created ({p.uid}). Next: enable Lakebase Search in the project Settings (UI).")


if __name__ == "__main__":
    main()
