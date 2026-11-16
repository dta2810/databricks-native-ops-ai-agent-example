"""Chat UI persistence on Lakebase: schema agent_ui with threads + messages (run as the project owner; idempotent; full columns, no ALTERs).
Per-user isolation is in SQL (every query filters user_id from the token): group roles make current_user the group, so RLS can't split users.
Usage: python3 setup/53_lakebase_ui.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from lakebase_conn import connect

SCHEMA = "agent_ui"

DDL = [
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.threads (
          thread_id   uuid PRIMARY KEY,
          user_id     text NOT NULL,
          title       text NOT NULL,
          created_at  timestamptz NOT NULL DEFAULT now(),
          updated_at  timestamptz NOT NULL DEFAULT now(),
          deleted_at  timestamptz)""",
    f"CREATE INDEX IF NOT EXISTS threads_user ON {SCHEMA}.threads (user_id, updated_at DESC) WHERE deleted_at IS NULL",
    f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.messages (
          id           uuid PRIMARY KEY,
          thread_id    uuid NOT NULL REFERENCES {SCHEMA}.threads (thread_id),
          user_id      text NOT NULL,
          role         text NOT NULL CHECK (role IN ('user', 'assistant')),
          content      text NOT NULL DEFAULT '',
          status       text NOT NULL DEFAULT 'done' CHECK (status IN ('done', 'error')),
          error        text,
          tool_events  jsonb NOT NULL DEFAULT '[]'::jsonb,
          sources      jsonb NOT NULL DEFAULT '[]'::jsonb,
          approval     jsonb,
          created_at   timestamptz NOT NULL DEFAULT now())""",
    f"CREATE INDEX IF NOT EXISTS messages_thread ON {SCHEMA}.messages (thread_id, created_at)",
    # MLflow trace of the assistant turn + the user's thumbs (also logged to the trace as a HUMAN assessment)
    f"ALTER TABLE {SCHEMA}.messages ADD COLUMN IF NOT EXISTS trace_id text",
    f"ALTER TABLE {SCHEMA}.messages ADD COLUMN IF NOT EXISTS feedback jsonb",
    f"CREATE INDEX IF NOT EXISTS messages_trace ON {SCHEMA}.messages (trace_id) WHERE trace_id IS NOT NULL",
    f"COMMENT ON TABLE {SCHEMA}.threads IS 'Beverage Ops Agent chat threads per user (soft delete via deleted_at). thread_id is also the LangGraph checkpointer thread key.'",
    f"COMMENT ON TABLE {SCHEMA}.messages IS 'Rendered chat turns: text plus tool calls, sources and the approval card state, so the UI can re-render a resumed thread.'",
]


def main():
    with connect() as conn:
        for s in DDL:
            conn.execute(s)
        conn.commit()
        print(conn.execute("""SELECT table_name, count(*) AS cols FROM information_schema.columns
                              WHERE table_schema = %s GROUP BY table_name ORDER BY 1""", (SCHEMA,)).fetchall())


if __name__ == "__main__":
    main()
