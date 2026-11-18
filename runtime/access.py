"""Application identities at the Databricks Apps and local HTTP boundaries."""

import base64
import os
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from databricks_agentkit import workspace_client, workspace_headers
from databricks_agentkit.runtime.auth import AuthError, RequestAuthContext
from databricks_agentkit.runtime.store import RUNTIME_STORE_LOCAL_ENV
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse


def deployed() -> bool:
    if os.getenv(RUNTIME_STORE_LOCAL_ENV, "").lower() == "true":
        return False
    url = urlsplit(os.getenv("DATABRICKS_APP_URL", ""))
    return bool(os.getenv("DATABRICKS_APP_NAME") and url.scheme == "https" and url.hostname)


@dataclass(frozen=True)
class Viewer:
    actor: str
    admin: bool

    def reading(self, actor: str | None) -> str:
        if actor and actor != self.actor and not self.admin:
            raise HTTPException(403, "Access to another user's data is restricted.")
        return actor or self.actor

    def require_admin(self) -> None:
        if not self.admin:
            raise HTTPException(403, "Application administrator required.")


def session_key(value: str | None) -> str:
    # These IDs also become path components in the managed Session Store API.
    if not value or value in (".", "..") or not value.isprintable() or any(c in value for c in "/\\%?#"):
        raise HTTPException(400, "Invalid or missing session_id")
    return value


def _login_required() -> HTTPException:
    return HTTPException(401, "Valid workspace credentials required.", headers={
        "WWW-Authenticate": 'Basic realm="Local agent (workspace access token)"'
    })


@lru_cache(maxsize=1)
def _workspace_host() -> str:
    host = workspace_client().config.host.rstrip("/")
    if urlsplit(host).scheme != "https":
        raise HTTPException(503, "Workspace identity verification requires HTTPS.")
    return host


async def _local_actor(request: Request) -> str:
    # The dev proxy supplies the developer's display headers to EVERY browser.
    # Verify the caller's own token instead. Basic supports the browser login dialog.
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "basic":
        try:
            _, separator, credential = base64.b64decode(credential, validate=True).decode().partition(":")
            if not separator:
                raise _login_required()
        except ValueError:
            raise _login_required() from None
    elif scheme.lower() != "bearer":
        raise _login_required()
    if not credential or not credential.isascii() or not credential.isprintable():
        raise _login_required()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                _workspace_host() + "/api/2.0/preview/scim/v2/Me",
                headers={**workspace_headers(), "Authorization": f"Bearer {credential}"},
            )
        if response.status_code in (401, 403):
            raise _login_required()
        response.raise_for_status()
        profile = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Workspace identity verification unavailable.") from None
    actor = profile.get("userName") if isinstance(profile, dict) else None
    if not isinstance(actor, str) or not actor.strip() or profile.get("active") is False:
        raise _login_required()
    return actor


async def authenticate(request: Request) -> Viewer:
    if deployed():
        # The production listener is behind the authenticated Databricks Apps ingress.
        for header in ("x-forwarded-email", "x-forwarded-user"):
            values = request.headers.getlist(header)
            if len(values) > 1 or any("," in value for value in values):
                raise HTTPException(401, "Ambiguous App identity.")
        actor = request.headers.get("x-forwarded-email") or request.headers.get("x-forwarded-user")
        if not actor or not actor.strip():
            raise HTTPException(401, "Authenticated App identity required.")
    else:
        actor = await _local_actor(request)
    admins = {v.strip() for v in os.getenv("AGENT_ADMIN_USERS", "").split(",") if v.strip()}
    return Viewer(actor.strip(), actor.strip() in admins)


@dataclass(frozen=True)
class RequestAccess:
    viewer: Viewer
    checkpoint_actor: str
    read_key: str | None = None


current_access: ContextVar[RequestAccess] = ContextVar("agent_request_access")


def read_namespace(request: Request) -> RequestAuthContext:
    target = request.query_params.get("principal_id")
    if target:
        # Namespace calculation only. Never construct a tool client for a target user.
        request.state.viewer.require_admin()
        return RequestAuthContext(token=None, principal=target, local=False)
    return RequestAuthContext.from_headers(request.headers)


async def authenticate_request(request: Request, call_next):
    if request.url.path != "/" and not request.url.path.startswith("/api/"):
        return await call_next(request)
    try:
        viewer = request.state.viewer = await authenticate(request)
        origin = request.headers.get("origin")
        # The local CLI proxy rewrites Host. Browsers attest same-origin fetches via
        # Fetch Metadata even when the public port differs from the backend port.
        if (
            not deployed() and request.method == "POST" and origin
            and request.headers.get("sec-fetch-site") != "same-origin"
            and urlsplit(origin).netloc != request.headers.get("host")
        ):
            raise HTTPException(403, "Cross-origin write denied.")
        checkpoint_actor = viewer.actor
        read_key = None
        if request.app.auth_policy.requires_user and request.url.path.startswith("/api/invocations"):
            auth = RequestAuthContext.from_headers(request.headers)
            try:
                checkpoint_actor = auth.namespace("actor", viewer.actor)
            finally:
                auth.close()
            if request.method == "GET" and request.query_params.get("principal_id"):
                auth = read_namespace(request)
                try:
                    public_id = UUID(request.url.path.split("/")[3])
                    read_key = auth.namespace("invocation", str(public_id))
                except (ValueError, IndexError):
                    raise HTTPException(400, "Invalid invocation ID.") from None
                finally:
                    auth.close()
            if request.method == "POST" and not deployed():
                # The SDK uses the dev profile for local OBO tool clients.
                if viewer.actor != workspace_client().current_user.me().user_name:
                    raise HTTPException(403, "Local user-authorized tools require the profile owner.")
    except AuthError as exc:
        return JSONResponse({"error": exc.payload()}, status_code=exc.status_code)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    token = current_access.set(RequestAccess(viewer, checkpoint_actor, read_key))
    try:
        return await call_next(request)
    finally:
        current_access.reset(token)
