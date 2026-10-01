from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Final

import aiohttp
from pydantic import ValidationError

from app.core.auth.models import OAuthTokenPayload
from app.core.clients.codex import (
    CodexClient,
    CodexTransportError,
    create_codex_session,
    require_route_or_direct_egress_opt_in,
)
from app.core.clients.http import _safe_json, lease_http_session
from app.core.config.settings import (
    AUTH_BASE_URL,
    OAUTH_AUTHORIZE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    OAUTH_TOKEN_PATH,
)
from app.core.types import JsonObject
from app.core.upstream_proxy import ResolvedUpstreamRoute
from app.core.utils.request_id import get_request_id

logger = logging.getLogger(__name__)

# Total timeout of one OAuth HTTP exchange (token, refresh via the
# authorization-code path); fixed since issue #1340 / PRINCIPLES.md P2. Callers
# may still pass an explicit ``timeout_seconds``.
OAUTH_TIMEOUT_SECONDS: Final[float] = 30.0


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    # Anthropic's OAuth flow issues no id_token; identity arrives as
    # organization/account uuid fields on the token response instead.
    id_token: str | None
    expires_in: int | None = None
    scope: str | None = None
    organization_id: str | None = None
    account_id: str | None = None
    email: str | None = None


class OAuthError(Exception):
    def __init__(self, code: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    return verifier, pkce_challenge(verifier)


def build_authorization_url(
    *,
    state: str,
    code_challenge: str,
    authorize_url: str | None = None,
    client_id: str | None = None,
    redirect_uri: str | None = None,
    scope: str | None = None,
) -> str:
    """Claude Code's authorize URL. ``code=true`` selects the copy/paste
    callback mode: the browser shows the code instead of calling a local
    redirect server, and the operator pastes ``CODE#STATE`` back."""
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": client_id or OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri or OAUTH_REDIRECT_URI,
        "scope": scope or OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "code": "true",
    }
    base = authorize_url or OAUTH_AUTHORIZE_URL
    return f"{base}?{urlencode(params)}"


def split_pasted_callback(value: str) -> tuple[str, str | None]:
    """Split a pasted callback value into ``(code, state)``.

    Anthropic's copy/paste flow returns ``CODE#STATE``; a bare code is also
    accepted (state None, caller falls back to the stored verifier)."""
    cleaned = value.strip()
    if "#" in cleaned:
        code, _, state = cleaned.partition("#")
        return code.strip(), state.strip() or None
    return cleaned, None


async def exchange_authorization_code(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str | None = None,
    token_url: str | None = None,
    client_id: str | None = None,
    timeout_seconds: float | None = None,
    session: aiohttp.ClientSession | None = None,
    route: ResolvedUpstreamRoute | None = None,
    codex_client: CodexClient | None = None,
    allow_direct_egress: bool = False,
) -> OAuthTokens:
    """Exchange an authorization code for tokens.

    Anthropic takes a JSON body (not form-encoded) and no client secret."""
    url = token_url or f"{AUTH_BASE_URL.rstrip('/')}{OAUTH_TOKEN_PATH}"
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id or OAUTH_CLIENT_ID,
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri or OAUTH_REDIRECT_URI,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds or OAUTH_TIMEOUT_SECONDS)

    headers = {"Content-Type": "application/json"}
    request_id = get_request_id()
    if request_id:
        headers["x-request-id"] = request_id
    require_route_or_direct_egress_opt_in(
        route=route,
        allow_direct_egress=allow_direct_egress,
        operation="OAuth token exchange",
    )
    if route is not None:
        resp = await _codex_post(
            url,
            route=route,
            codex_client=codex_client,
            json=payload,
            headers=headers,
            timeout=timeout_seconds or OAUTH_TIMEOUT_SECONDS,
        )
        data = await _safe_codex_json(resp)
        token_payload = _validate_oauth_token_payload(data, "OAuth token response invalid")
        status = _codex_status(resp)
        if status >= 400:
            logger.warning("OAuth token request failed request_id=%s status=%s", get_request_id(), status)
            raise _oauth_error_from_payload(token_payload, status)
        return _parse_tokens(token_payload)
    async with lease_http_session(session) as client_session:
        async with client_session.post(
            url,
            json=payload,
            headers=headers,
            timeout=timeout,
        ) as resp:
            data = await _safe_json(resp)
            try:
                token_payload = OAuthTokenPayload.model_validate(data)
            except ValidationError as exc:
                logger.warning(
                    "OAuth token response invalid request_id=%s",
                    get_request_id(),
                )
                raise OAuthError("invalid_response", "OAuth response invalid") from exc
            if resp.status >= 400:
                logger.warning(
                    "OAuth token request failed request_id=%s status=%s",
                    get_request_id(),
                    resp.status,
                )
                raise _oauth_error_from_payload(token_payload, resp.status)

    return _parse_tokens(token_payload)


def _parse_tokens(payload: OAuthTokenPayload) -> OAuthTokens:
    if not payload.access_token:
        raise OAuthError("invalid_response", "OAuth response missing tokens")
    return OAuthTokens(
        access_token=payload.access_token,
        refresh_token=payload.refresh_token,
        id_token=payload.id_token,
        expires_in=payload.expires_in,
        scope=payload.scope,
        organization_id=payload.organization_uuid,
        account_id=payload.account_uuid,
        email=payload.email,
    )


async def _codex_post(
    url: str,
    *,
    route: ResolvedUpstreamRoute,
    codex_client: CodexClient | None,
    **kwargs: Any,
) -> Any:
    owns_codex_client = codex_client is None
    active_codex_client = codex_client or CodexClient(create_codex_session())
    try:
        return await active_codex_client.request("POST", url, route=route, **kwargs)
    except CodexTransportError as exc:
        raise OAuthError("transport_error", str(exc)) from exc
    finally:
        if owns_codex_client:
            await active_codex_client.close()


def _codex_status(resp: Any) -> int:
    return int(getattr(resp, "status_code", getattr(resp, "status", 0)))


async def _safe_codex_json(resp: Any) -> JsonObject:
    json_method = getattr(resp, "json", None)
    try:
        if callable(json_method):
            data = json_method()
            if hasattr(data, "__await__"):
                data = await data
        else:
            text = getattr(resp, "text", "")
            data = __import__("json").loads(text)
    except Exception:
        text = getattr(resp, "text", "")
        return {"error": {"message": str(text).strip()}}
    return data if isinstance(data, dict) else {"error": {"message": str(data)}}


def _validate_oauth_token_payload(data: JsonObject, message: str) -> OAuthTokenPayload:
    try:
        return OAuthTokenPayload.model_validate(data)
    except ValidationError as exc:
        logger.warning("%s request_id=%s", message, get_request_id())
        raise OAuthError("invalid_response", message) from exc


def _oauth_error_from_payload(payload: OAuthTokenPayload, status_code: int) -> OAuthError:
    code = _extract_error_code(payload) or f"http_{status_code}"
    message = _extract_error_message(payload) or f"OAuth request failed ({status_code})"
    return OAuthError(code, message, status_code)


def _extract_error_code(payload: OAuthTokenPayload) -> str | None:
    error = payload.error
    if isinstance(error, dict):
        code = error.get("code") or error.get("error")
        return code if isinstance(code, str) else None
    if isinstance(error, str):
        return error
    return payload.error_code or payload.code


def _extract_error_message(payload: OAuthTokenPayload) -> str | None:
    error = payload.error
    if isinstance(error, dict):
        message = error.get("message") or error.get("error_description")
        return message if isinstance(message, str) else None
    if isinstance(error, str):
        return payload.error_description or error
    return payload.message
