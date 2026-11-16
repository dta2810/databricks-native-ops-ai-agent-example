"""Who is calling: identity from the forwarded user token (cross-checked with x-forwarded-email) and the groups they hold."""
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from server import auth
from server import settings as S

logger = logging.getLogger(__name__)

_identity_cache: dict = {}  # sha256(token)[:16] -> (time, email)
_IDENTITY_TTL, _IDENTITY_MAX = 600, 1000
_roles_cache: dict = {}  # email -> (time, roles)
_ROLES_TTL = 300

_SCRUB = [
    (re.compile(r"\.?\s*Config:.*", re.S), ""),
    (re.compile(r"\s*Env:.*", re.S), ""),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"(eyJ[A-Za-z0-9_-]{10})[A-Za-z0-9_.-]+"), r"\1<redacted>"),
    (re.compile(r"\b[\w.-]+\.(?:azure)?databricks\.(?:net|com)\b", re.I), "<host>"),
    (re.compile(r"\bpostgresql://[^\s]+"), "<connection>"),
]


def clean(msg: str, limit: int = 300) -> str:
    # strip hosts, tokens and IPs before logging or returning an error
    for pat, repl in _SCRUB:
        msg = pat.sub(repl, msg)
    msg = " ".join(msg.split())
    return msg[:limit] + ("…" if len(msg) > limit else "")


@dataclass
class User:
    email: str
    token: str | None  # None only in LOCAL_DEV
    roles: list[str] = field(default_factory=list)

    @property
    def can_approve(self) -> bool:
        return S.APPROVER_GROUP in self.roles

    @property
    def can_propose(self) -> bool:
        return any(S.ROLES[r]["can_propose"] for r in self.roles if r in S.ROLES)

    @property
    def is_manager(self) -> bool:
        return S.MANAGER_GROUP in self.roles

    def service(self):
        # app SP connection for writes: only call after checking the caller's role
        return auth.service_connection()

    @property
    def read_role(self) -> str | None:
        # any held role can read; prefer the least privileged
        for r in (S.PLANNER_GROUP, S.APPROVER_GROUP):
            if r in self.roles:
                return r
        return None

    def client(self):
        return auth.user_client(self.token)

    def connection(self, role: str):
        return auth.connection(self.email, role, self.token)


def _email(request: Request) -> tuple[str | None, str]:
    token = request.headers.get("x-forwarded-access-token")
    if not token:
        if S.LOCAL_DEV:
            if "email" not in _identity_cache:
                _identity_cache["email"] = auth.local_client().current_user.me().user_name
            return None, _identity_cache["email"]
        raise HTTPException(401, "Sign-in required (no forwarded user token)")
    key, now = hashlib.sha256(token.encode()).hexdigest()[:16], time.time()
    hit = _identity_cache.get(key)
    if hit and now - hit[0] < _IDENTITY_TTL:
        email = hit[1]
    else:
        try:
            email = auth.user_client(token).current_user.me().user_name
        except Exception as e:
            logger.error("identity lookup failed: %s", type(e).__name__)
            raise HTTPException(401, "Invalid or expired token")
        if len(_identity_cache) >= _IDENTITY_MAX:
            _identity_cache.pop(min((k for k in _identity_cache if k != "email"), key=lambda k: _identity_cache[k][0]))
        _identity_cache[key] = (now, email)
    header = request.headers.get("x-forwarded-email")
    if header and header.lower() != email.lower():
        logger.warning("identity mismatch between x-forwarded-email and token")
        raise HTTPException(401, "Identity mismatch")
    return token, email


def _roles(token: str | None, email: str) -> list[str]:
    if token is None:
        return list(S.ROLES)  # local dev: the CLI user acts in every role
    now = time.time()
    hit = _roles_cache.get(email)
    if hit and now - hit[0] < _ROLES_TTL:
        return hit[1]
    out = []
    for group in S.ROLES:
        try:
            auth.mint(token, group)  # the platform refuses the mint when the user is not a member
            out.append(group)
        except Exception as e:
            logger.info("mint for %s refused: %s", group, type(e).__name__)
    if S.MANAGER_GROUP not in S.ROLES and _in_group(token, S.MANAGER_GROUP):
        out.append(S.MANAGER_GROUP)  # approval authority only (no Lakebase role of its own)
    if out:  # an empty result may be transient (cold start): do not cache it
        _roles_cache[email] = (now, out)
    return out


def _in_group(token: str, group: str) -> bool:
    # the user's own workspace groups, read with their token (direct memberships; the platform is the source)
    try:
        me = auth.user_client(token).current_user.me()
    except Exception as e:
        logger.info("group lookup for %s failed: %s", group, type(e).__name__)
        return False
    return any((g.display or "") == group for g in (me.groups or []))


def current_user(request: Request) -> User:
    token, email = _email(request)
    return User(email=email, token=token, roles=_roles(token, email))


def require_role(user: User) -> str:
    role = user.read_role
    if role is None:
        raise HTTPException(403, f"Ask an administrator to add you to {S.PLANNER_GROUP} or {S.APPROVER_GROUP}")
    return role
