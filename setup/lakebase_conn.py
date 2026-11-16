"""Lakebase connection helper (OAuth credential via the SDK, profile from config). Usage: python3 setup/lakebase_conn.py probe"""
import os
import time
from functools import lru_cache
import sys

import psycopg

sys.path.insert(0, os.path.dirname(__file__))
import config as C

BRANCH = os.environ.get("BEV_LAKEBASE_BRANCH", "production")
ENDPOINT = os.environ.get("BEV_LAKEBASE_ENDPOINT", "primary")
DATABASE = os.environ.get("BEV_LAKEBASE_DATABASE", "databricks_postgres")


@lru_cache(maxsize=1)
def _wc():
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient(profile=C.PROFILE)


def endpoint_name():
    return f"projects/{C.LAKEBASE_PROJECT}/branches/{BRANCH}/endpoints/{ENDPOINT}"


_TOKEN_TTL_S = 30 * 60
_cache = {}


def _target():
    # host + user are stable; the OAuth credential is refreshed every 30 min
    if "host" not in _cache:
        _cache["host"] = _wc().postgres.get_endpoint(endpoint_name()).status.hosts.host
        _cache["user"] = _wc().current_user.me().user_name
    if time.time() - _cache.get("token_at", 0) > _TOKEN_TTL_S:
        _cache["token"] = _wc().postgres.generate_database_credential(endpoint=endpoint_name()).token
        _cache["token_at"] = time.time()
    return _cache["host"], _cache["user"], _cache["token"]


def connect(dbname=DATABASE):
    host, user, token = _target()
    return psycopg.connect(host=host, port=5432, dbname=dbname, user=user, password=token, sslmode="require", connect_timeout=30)


def probe():
    with connect() as conn:
        print(conn.execute("select version()").fetchone()[0])
        rows = conn.execute(
            "select name, default_version, installed_version from pg_available_extensions "
            "where name in ('vector','lakebase_vector','lakebase_text','pg_trgm') order by name"
        ).fetchall()
        for r in rows:
            print(r)


if __name__ == "__main__":
    {"probe": probe}[sys.argv[1] if len(sys.argv) > 1 else "probe"]()
