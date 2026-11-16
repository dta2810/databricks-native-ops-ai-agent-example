"""Create the secret scope and a random trace salt for this environment (idempotent; never overwrites).

The app hashes user identities in MLflow traces and derives thread keys with HMAC(salt, email). The salt must stay
secret (a public value lets trace readers re-identify users) and stable (a new salt orphans existing threads).
Usage: BEV_TARGET=<target> python setup/trace_salt.py [--apply] [--rotate]
"""
import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

KEY = "trace-salt"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rotate", action="store_true", help="replace the salt (existing chat threads stop resuming)")
    a = ap.parse_args()
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient(profile=C.PROFILE)
    scopes = {s.name for s in w.secrets.list_scopes()}
    have = C.SECRET_SCOPE in scopes and any(k.key == KEY for k in w.secrets.list_secrets(C.SECRET_SCOPE))
    if have and not a.rotate:
        print(f"secret {C.SECRET_SCOPE}/{KEY}: exists (kept)")
        return
    print(f"-> {'create scope ' + C.SECRET_SCOPE + ' and ' if C.SECRET_SCOPE not in scopes else ''}write a random {KEY}")
    if not a.apply:
        print("dry run: add --apply")
        return
    if C.SECRET_SCOPE not in scopes:
        w.secrets.create_scope(C.SECRET_SCOPE)
    w.secrets.put_secret(C.SECRET_SCOPE, KEY, string_value=secrets.token_urlsafe(32))  # value never printed
    print(f"secret {C.SECRET_SCOPE}/{KEY}: written")


if __name__ == "__main__":
    main()
