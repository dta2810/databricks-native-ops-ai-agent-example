"""Beverage Ops Agent: Databricks App entry point (FastAPI API + React SPA)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# shared packages (memory/, rag/, agent/) are staged into the app at deploy; locally they live at the repo root
if not (HERE / "memory").exists():
    sys.path.insert(1, str(HERE.parent))

from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402

from server.analytics import router as analytics_router  # noqa: E402
from server.chat import router as chat_router, start_bridge, stop_bridge  # noqa: E402
from server.prompts import router as prompts_router  # noqa: E402
from server.routes import router  # noqa: E402



@asynccontextmanager
async def lifespan(_app):
    # one agent bridge + Lakebase checkpoint pool per process
    start_bridge()
    try:
        yield
    finally:
        stop_bridge()
        from server.auth import close_pools
        close_pools()


from server.settings import IN_APPS  # noqa: E402
from agent.tracing import require_salt  # noqa: E402

# masked user ids in traces need the secret salt: refuse to start in Apps without it
require_salt(IN_APPS)

# deployed app: no interactive API docs / schema (smaller surface); local dev keeps them (reference: app/API.md)
_docs = {} if not IN_APPS else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="Beverage Ops Agent", lifespan=lifespan, **_docs)


CSP = ("default-src 'self'; img-src 'self' data:; connect-src 'self'; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'")


class CacheControlMiddleware(BaseHTTPMiddleware):
    # API responses are per-user: never cache unless a route says otherwise
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "private, no-store"
        # no third-party loads: blocks data exfiltration through injected markdown images/links
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def csrf_ok(method: str, path: str, headers) -> bool:
    """Cross-site request guard for state-changing API calls (the session cookie rides along on cross-site requests).
    JSON content type is required (a cross-site JSON POST needs a CORS preflight, which this app never grants);
    when the browser says where the request came from (Sec-Fetch-Site / Origin), it must be this app."""
    if method in SAFE_METHODS or not path.startswith("/api/"):
        return True
    if not (headers.get("content-type") or "").lower().startswith("application/json"):
        return False
    site = (headers.get("sec-fetch-site") or "").lower()
    if site:
        return site in ("same-origin", "none")
    origin = headers.get("origin")
    if origin:
        host = headers.get("x-forwarded-host") or headers.get("host") or ""
        return origin.split("://", 1)[-1].rstrip("/").lower() == host.lower()
    return True  # no browser provenance headers: not a browser cross-site request


class CsrfMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if not csrf_ok(request.method, request.url.path, request.headers):
            from starlette.responses import JSONResponse
            return JSONResponse({"detail": "Cross-site or non-JSON request refused"}, status_code=403)
        return await call_next(request)


app.add_middleware(CsrfMiddleware)
app.add_middleware(CacheControlMiddleware)
app.include_router(router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(analytics_router, prefix="/api")
app.include_router(prompts_router, prefix="/api")

frontend_dist = HERE / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")
    dist_root = frontend_dist.resolve()

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # resolve + containment check: an absolute or ../ path must never leave dist/
        file_path = (dist_root / full_path.lstrip("/")).resolve()
        if file_path.is_relative_to(dist_root) and file_path.is_file():
            return FileResponse(file_path)
        # SPA shell must revalidate, or browsers keep an old bundle after a deploy (assets are hashed)
        return FileResponse(dist_root / "index.html", headers={"Cache-Control": "no-cache"})
