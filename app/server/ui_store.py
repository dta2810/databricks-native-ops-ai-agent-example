"""Chat threads + rendered messages in Lakebase agent_ui (setup/53_lakebase_ui.py).
Stored through the app SP after the caller's group membership is checked: group roles have no access to agent_ui, so a user
holding a group credential cannot read other people's chats. Every query filters user_id from the token; a thread that isn't
the caller's behaves as not found.
"""
import json
import uuid

from psycopg.rows import dict_row

from memory.pgretry import retrying

from server.identity import User, require_role

S = "agent_ui"
DEFAULT_TITLE = "New conversation"


def _j(v):
    return None if v is None else json.dumps(v, default=str)


def _conn(user: User):
    require_role(user)
    return user.service()


def is_uuid(v: str) -> bool:
    try:
        uuid.UUID(v)
        return True
    except (ValueError, TypeError):
        return False


@retrying
def list_threads(user: User, q: str | None = None, limit: int = 50):
    with _conn(user) as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""SELECT t.thread_id::text, t.title, t.created_at, t.updated_at,
                               EXISTS (SELECT 1 FROM {S}.messages m WHERE m.thread_id = t.thread_id AND m.user_id = t.user_id
                                       AND m.approval->>'state' = 'pending') AS pending_approval
                        FROM {S}.threads t
                        WHERE t.user_id = %s AND t.deleted_at IS NULL AND (%s::text IS NULL OR t.title ILIKE %s)
                        ORDER BY t.updated_at DESC LIMIT %s""",
                    (user.email, q, f"%{q}%" if q else None, min(max(limit, 1), 100)))
        return cur.fetchall()


@retrying
def get_thread(user: User, thread_id: str):
    if not is_uuid(thread_id):
        return None
    with _conn(user) as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT thread_id::text, title, created_at, updated_at FROM {S}.threads "
                    "WHERE thread_id = %s AND user_id = %s AND deleted_at IS NULL", (thread_id, user.email))
        return cur.fetchone()


@retrying
def create_thread(user: User, title: str | None = None, thread_id: str | None = None):
    tid = thread_id or str(uuid.uuid4())
    with _conn(user) as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""INSERT INTO {S}.threads (thread_id, user_id, title) VALUES (%s, %s, %s)
                        ON CONFLICT (thread_id) DO NOTHING
                        RETURNING thread_id::text, title, created_at, updated_at""",
                    (tid, user.email, (title or DEFAULT_TITLE)[:120]))
        row = cur.fetchone()
        c.commit()
        return row


@retrying
def rename_thread(user: User, thread_id: str, title: str) -> bool:
    with _conn(user) as c:
        n = c.execute(f"UPDATE {S}.threads SET title = %s, updated_at = now() "
                      "WHERE thread_id = %s AND user_id = %s AND deleted_at IS NULL", (title[:120], thread_id, user.email)).rowcount
        c.commit()
        return n > 0


@retrying
def delete_thread(user: User, thread_id: str) -> bool:
    with _conn(user) as c:
        n = c.execute(f"UPDATE {S}.threads SET deleted_at = now() WHERE thread_id = %s AND user_id = %s AND deleted_at IS NULL",
                      (thread_id, user.email)).rowcount
        c.commit()
        return n > 0


@retrying
def messages(user: User, thread_id: str):
    with _conn(user) as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""SELECT m.id::text, m.role, m.content, m.status, m.error, m.tool_events, m.sources, m.approval, m.trace_id, m.feedback, m.created_at
                        FROM {S}.messages m JOIN {S}.threads t ON t.thread_id = m.thread_id
                        WHERE m.thread_id = %s AND m.user_id = %s AND t.user_id = %s AND t.deleted_at IS NULL
                        ORDER BY m.created_at, m.id""", (thread_id, user.email, user.email))
        return cur.fetchall()


@retrying
def add_user_message(user: User, thread_id: str, text: str):
    # first user message becomes the title while the thread still has the default one
    with _conn(user) as c:
        c.execute(f"INSERT INTO {S}.messages (id, thread_id, user_id, role, content) VALUES (%s, %s, %s, 'user', %s)",
                  (str(uuid.uuid4()), thread_id, user.email, text))
        c.execute(f"""UPDATE {S}.threads SET updated_at = now(),
                        title = CASE WHEN title = %s THEN left(regexp_replace(%s, '\\s+', ' ', 'g'), 80) ELSE title END
                      WHERE thread_id = %s AND user_id = %s""", (DEFAULT_TITLE, text.strip(), thread_id, user.email))
        c.commit()


@retrying
def add_assistant_message(user: User, thread_id: str, turn: dict) -> str:
    mid = str(uuid.uuid4())
    with _conn(user) as c:
        c.execute(f"""INSERT INTO {S}.messages (id, thread_id, user_id, role, content, status, error, tool_events, sources, approval, trace_id)
                      VALUES (%s, %s, %s, 'assistant', %s, %s, %s, %s, %s, %s, %s)""",
                  (mid, thread_id, user.email, turn.get("text", ""), "error" if turn.get("error") else "done", turn.get("error"),
                   _j(turn.get("tools", [])), _j(turn.get("sources", [])), _j(turn.get("approval")), turn.get("trace_id")))
        c.execute(f"UPDATE {S}.threads SET updated_at = now() WHERE thread_id = %s AND user_id = %s", (thread_id, user.email))
        c.commit()
    return mid


@retrying
def set_feedback(user: User, thread_id: str, trace_id: str, feedback: dict) -> bool:
    # only the caller's own assistant message in the caller's live thread
    with _conn(user) as c:
        cur = c.execute(f"""UPDATE {S}.messages m SET feedback = %s
                            FROM {S}.threads t
                            WHERE t.thread_id = m.thread_id AND t.user_id = %s AND t.deleted_at IS NULL
                              AND m.thread_id = %s AND m.trace_id = %s AND m.user_id = %s AND m.role = 'assistant'""",
                        (_j(feedback), user.email, thread_id, trace_id, user.email))
        c.commit()
        return cur.rowcount > 0


@retrying
def get_feedback(user: User, thread_id: str, trace_id: str):
    with _conn(user) as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""SELECT m.feedback FROM {S}.messages m JOIN {S}.threads t ON t.thread_id = m.thread_id
                        WHERE t.user_id = %s AND t.deleted_at IS NULL AND m.thread_id = %s AND m.trace_id = %s
                          AND m.user_id = %s AND m.role = 'assistant'""", (user.email, thread_id, trace_id, user.email))
        row = cur.fetchone()
        return row["feedback"] if row else None


@retrying
def set_card_state_for_request(request_id: str, state: str, decided_by: str):
    # a decision taken on the Requests page updates the chat card of whoever proposed it (system write, app SP)
    from server.auth import service_connection
    with service_connection() as c:
        c.execute(f"""UPDATE {S}.messages SET approval = approval || jsonb_build_object('state', %s::text, 'decidedBy', %s::text)
                      WHERE approval->'request'->>'request_id' = %s AND approval->>'state' IN ('pending', 'queued', 'submitted')""",
                  (state, decided_by, str(request_id)))
        c.commit()


@retrying
def pending_approval(user: User, thread_id: str):
    with _conn(user) as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""SELECT id::text, approval FROM {S}.messages
                        WHERE thread_id = %s AND user_id = %s AND approval->>'state' = 'pending'
                        ORDER BY created_at DESC LIMIT 1""", (thread_id, user.email))
        return cur.fetchone()


@retrying
def queue_pending_cards(user: User, thread_id: str):
    with _conn(user) as c:
        c.execute(f"""UPDATE {S}.messages SET approval = approval || jsonb_build_object('state', 'queued')
                      WHERE thread_id = %s AND user_id = %s AND approval->>'state' = 'pending'""", (thread_id, user.email))
        c.commit()


@retrying
def set_approval_state(user: User, message_id: str, state: str, decided_by: str | None):
    with _conn(user) as c:
        c.execute(f"""UPDATE {S}.messages SET approval = approval || jsonb_build_object('state', %s::text, 'decidedBy', %s::text)
                      WHERE id = %s AND user_id = %s""", (state, decided_by, message_id, user.email))
        c.commit()
