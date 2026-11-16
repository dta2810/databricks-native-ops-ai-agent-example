"""Retry once when Lakebase drops a connection (suspend/restart -> AdminShutdown, closed socket). Wrap the unit of work that
opens its own connection, so the retry gets a fresh one from the pool. Not for writes with side effects outside Postgres."""
import functools
import logging
import time

import psycopg

logger = logging.getLogger(__name__)
RETRY_DELAY_S = 0.5


def is_transient(e: BaseException) -> bool:
    # AdminShutdown is an OperationalError; libraries may wrap it, so follow the cause chain
    seen = set()
    while e is not None and id(e) not in seen:
        if isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError)):
            return True
        seen.add(id(e))
        e = e.__cause__ or e.__context__
    return False


def retry_once(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if not is_transient(e):
            raise
        logger.warning("Lakebase connection dropped (%s): retrying %s once", type(e).__name__, getattr(fn, "__name__", fn))
        time.sleep(RETRY_DELAY_S)
        return fn(*args, **kwargs)


def retrying(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return retry_once(fn, *args, **kwargs)
    return wrapper
