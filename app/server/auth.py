"""OBO to Lakebase: group-scoped credentials minted with the user's forwarded token, pooled per (user, role).
LOCAL_DEV uses the CLI profile and setup/lakebase_conn.py instead.
"""
import threading
import time

import psycopg
from databricks.sdk import WorkspaceClient
from psycopg_pool import ConnectionPool

from server import settings as S

MAX_LIFETIME_S = 300  # recycle quickly so group revocation takes effect; credential expires in ~1h


class GovernanceError(RuntimeError):
    pass


def _host() -> str:
    # DATABRICKS_HOST in Apps has no scheme; the SDK needs one
    import os
    h = os.environ.get("DATABRICKS_HOST", "")
    return h if h.startswith("http") else f"https://{h}"


def user_client(user_token: str | None) -> WorkspaceClient:
    if user_token is None:
        return local_client()
    return WorkspaceClient(host=_host(), token=user_token, auth_type="pat")


_local: dict = {}


def local_client() -> WorkspaceClient:
    if not S.LOCAL_DEV:
        raise RuntimeError("no user token and LOCAL_DEV is off")
    if "w" not in _local:
        import os
        _local["w"] = WorkspaceClient(profile=os.environ.get("DATABRICKS_CONFIG_PROFILE") or None)
    return _local["w"]


def mint(user_token: str, group: str) -> str:
    # platform refuses when the user is not a member of the group
    return user_client(user_token).postgres.generate_database_credential(
        endpoint=S.LAKEBASE_ENDPOINT, group_name=group).token


_host_cache: dict = {}


def pg_host(user_token: str) -> str:
    if "h" not in _host_cache:
        _host_cache["h"] = user_client(user_token).postgres.get_endpoint(name=S.LAKEBASE_ENDPOINT).status.hosts.host
    return _host_cache["h"]


def governance_warnings(conn) -> list[str]:
    # never serve data on a connection that bypasses RLS or is a superuser
    row = conn.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    out = []
    if row and (row[1] or row[2]):
        out.append(f"{row[0]} bypasses row-level security")
    member = conn.execute("""SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
                             WHERE r.rolname = 'databricks_superuser'
                               AND m.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)""").fetchone()
    if member:
        out.append("current_user is a member of databricks_superuser")
    return out


def _guard(conn):
    w = governance_warnings(conn)
    conn.commit()
    if w:
        raise GovernanceError("; ".join(w))


class _RolePool:
    # one pool per (user, role); the latest forwarded token mints the password for each new connection
    def __init__(self, role: str, user_token: str):
        self.role = role
        self.token = user_token
        self.last_used = time.time()
        rp = self

        class _Conn(psycopg.Connection):
            @classmethod
            def connect(cls, conninfo="", **kw):
                kw["password"] = mint(rp.token, rp.role)
                return super().connect(conninfo, **kw)

        self.pool = ConnectionPool(
            kwargs={"host": pg_host(user_token), "dbname": S.PGDATABASE, "user": role,
                    "sslmode": "require", "connect_timeout": 30},
            connection_class=_Conn, configure=_guard, check=ConnectionPool.check_connection,
            min_size=0, max_size=4, max_idle=MAX_LIFETIME_S, max_lifetime=MAX_LIFETIME_S, open=True)


_pools: dict = {}  # (email, role) -> _RolePool
_lock = threading.Lock()
_POOL_IDLE_TIMEOUT_S = 15 * 60


_local_db = {}  # local dev: one pool + cached host/user/credential (CLI calls cost ~1-2 s each)
_LOCAL_TOKEN_TTL_S = 45 * 60


def _local_pool():
    with _lock:
        if "pool" in _local_db:
            return _local_db["pool"]
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "setup"))
        import lakebase_conn as L
        host, user, _ = L._target()

        def token():
            # the helper refreshes the OAuth credential itself
            return L._target()[2]

        class _Conn(psycopg.Connection):
            @classmethod
            def connect(cls, conninfo="", **kw):
                kw["password"] = token()
                return super().connect(conninfo, **kw)

        _local_db["pool"] = ConnectionPool(
            kwargs={"host": host, "port": 5432, "dbname": S.PGDATABASE, "user": user, "sslmode": "require", "connect_timeout": 30},
            connection_class=_Conn, check=ConnectionPool.check_connection,
            min_size=0, max_size=4, max_idle=MAX_LIFETIME_S, max_lifetime=MAX_LIFETIME_S, open=True)
        return _local_db["pool"]


_sp = {}  # app service principal: its own Lakebase role (writes after the app checked the caller's group)


def _sp_pool():
    with _lock:
        if "pool" in _sp:
            return _sp["pool"]
        import os
        w = WorkspaceClient()  # Apps runtime: SP credentials from env
        user = os.environ["DATABRICKS_CLIENT_ID"]
        host = w.postgres.get_endpoint(name=S.LAKEBASE_ENDPOINT).status.hosts.host

        def token():
            if time.time() - _sp.get("minted", 0) > _LOCAL_TOKEN_TTL_S:
                _sp["token"] = w.postgres.generate_database_credential(endpoint=S.LAKEBASE_ENDPOINT).token
                _sp["minted"] = time.time()
            return _sp["token"]

        class _Conn(psycopg.Connection):
            @classmethod
            def connect(cls, conninfo="", **kw):
                kw["password"] = token()
                return super().connect(conninfo, **kw)

        _sp["pool"] = ConnectionPool(
            kwargs={"host": host, "dbname": S.PGDATABASE, "user": user, "sslmode": "require", "connect_timeout": 30},
            connection_class=_Conn, configure=_guard, check=ConnectionPool.check_connection,
            min_size=0, max_size=8, max_idle=MAX_LIFETIME_S, max_lifetime=MAX_LIFETIME_S, open=True)
        return _sp["pool"]


def service_connection():
    # writes and per-user chat storage: app SP in Apps, the CLI owner locally. Callers authorize first.
    return _local_connection() if S.LOCAL_DEV else _sp_pool().connection()


def close_pools():
    # lifespan shutdown: stop pool worker threads cleanly
    with _lock:
        for holder in (_local_db, _sp):
            p = holder.pop("pool", None)
            if p is not None:
                p.close()
        for k in list(_pools):
            _pools.pop(k).pool.close()


def _local_connection():
    # local dev: the CLI user's own credential (owner of the demo project), pooled
    return _local_pool().connection()


def connection(user_email: str, role: str, user_token: str | None):
    if user_token is None:
        return _local_connection()
    with _lock:
        now = time.time()
        for k in [k for k, p in _pools.items() if now - p.last_used > _POOL_IDLE_TIMEOUT_S]:
            _pools.pop(k).pool.close()
        rp = _pools.get((user_email, role))
        if rp is None:
            rp = _pools[(user_email, role)] = _RolePool(role, user_token)
        rp.token, rp.last_used = user_token, now
    return rp.pool.connection()
