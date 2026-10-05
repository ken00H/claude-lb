"""Anthropic Messages API upstream client.

Talks to ``https://api.anthropic.com/v1/messages`` over the same route-resolved
transport the other upstream clients use (``CodexClient`` is provider-neutral
despite its name; claude.ai/anthropic hosts are region-blocked on some
deployments, so egress goes through the configured upstream proxy route).

Authentication is selected purely by the account's ``pool_class``:
``oauth_seat`` sends a Bearer token plus Claude Code impersonation headers;
``api_key`` sends ``x-api-key`` plus ``anthropic-version``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

import aiohttp

from app.core.anthropic.models import parse_anthropic_error_payload
from app.core.clients.codex import (
    CodexClient,
    CodexTransportError,
    create_codex_session,
)
from app.core.clients.http import lease_http_session
from app.core.config.settings import (
    ANTHROPIC_API_BASE_URL,
    ANTHROPIC_MESSAGES_PATH,
    ANTHROPIC_VERSION,
    CLAUDE_CODE_APP_HEADER,
    CLAUDE_CODE_OAUTH_BETA,
    CLAUDE_CODE_USER_AGENT,
)
from app.core.types import JsonObject
from app.core.upstream_proxy import ResolvedUpstreamRoute

logger = logging.getLogger(__name__)

# Total timeout of a non-streaming Messages call; streaming calls keep the
# connection open for the model's runtime and bound only connect + idle reads.
MESSAGES_REQUEST_TIMEOUT_SECONDS: Final[float] = 600.0
MESSAGES_STREAM_CONNECT_TIMEOUT_SECONDS: Final[float] = 30.0
MESSAGES_STREAM_IDLE_TIMEOUT_SECONDS: Final[float] = 300.0
_STREAM_CHUNK_BYTES: Final[int] = 8192


class AnthropicUpstreamError(Exception):
    """A non-2xx Messages API response carrying the Anthropic error envelope."""

    def __init__(
        self, status_code: int, error_type: str, message: str, *, retry_after_seconds: int | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class MessagesResult:
    status_code: int
    headers: Mapping[str, str]
    data: JsonObject


def build_messages_headers(pool_class: str, access_token: str) -> dict[str, str]:
    """Per-pool-class upstream authentication headers for /v1/messages."""
    headers: dict[str, str] = {
        "content-type": "application/json",
        "accept": "application/json",
    }
    if pool_class == "api_key":
        headers["x-api-key"] = access_token
        headers["anthropic-version"] = ANTHROPIC_VERSION
        return headers
    # oauth_seat (the default): authenticate as the Claude Code CLI.
    headers["authorization"] = f"Bearer {access_token}"
    headers["anthropic-version"] = ANTHROPIC_VERSION
    headers["anthropic-beta"] = CLAUDE_CODE_OAUTH_BETA
    headers["user-agent"] = CLAUDE_CODE_USER_AGENT
    headers["x-app"] = CLAUDE_CODE_APP_HEADER
    return headers


def _messages_url() -> str:
    return f"{ANTHROPIC_API_BASE_URL.rstrip('/')}{ANTHROPIC_MESSAGES_PATH}"


async def _raise_from_response(resp: Any) -> None:
    status = int(getattr(resp, "status", getattr(resp, "status_code", 0)))
    data = await _response_json(resp)
    error_type, message = parse_anthropic_error_payload(data)
    raise AnthropicUpstreamError(
        status,
        error_type or f"http_{status}",
        message or "Anthropic upstream request failed",
        retry_after_seconds=_retry_after_seconds(resp),
    )


def _retry_after_seconds(resp: Any) -> int | None:
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return None


async def _response_json(resp: Any) -> JsonObject:
    text_method = getattr(resp, "text", None)
    try:
        if callable(text_method):
            text = text_method()
            if hasattr(text, "__await__"):
                text = await text
        else:
            body = getattr(resp, "body", b"")
            text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def _codex_request(
    method: str,
    *,
    route: ResolvedUpstreamRoute,
    codex_client: CodexClient | None,
    buffer_response: bool,
    **kwargs: Any,
) -> Any:
    owns_codex_client = codex_client is None
    active_codex_client = codex_client or CodexClient(create_codex_session())
    try:
        return await active_codex_client.request(
            method,
            kwargs.pop("url"),
            route=route,
            buffer_response=buffer_response,
            **kwargs,
        )
    except CodexTransportError as exc:
        raise AnthropicUpstreamError(502, "api_error", f"Anthropic upstream transport error: {exc}") from exc
    finally:
        if owns_codex_client:
            await active_codex_client.close()


async def request_messages(
    payload: Mapping[str, Any],
    *,
    pool_class: str,
    access_token: str,
    route: ResolvedUpstreamRoute | None = None,
    codex_client: CodexClient | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout_seconds: float | None = None,
    allow_direct_egress: bool = False,
) -> MessagesResult:
    """Non-streaming Messages call. Returns status, response headers, and body."""
    url = _messages_url()
    headers = build_messages_headers(pool_class, access_token)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds or MESSAGES_REQUEST_TIMEOUT_SECONDS)
    if route is not None:
        resp = await _codex_request(
            "POST",
            url=url,
            route=route,
            codex_client=codex_client,
            buffer_response=True,
            json=dict(payload),
            headers=headers,
            timeout=timeout,
        )
        data = await _response_json(resp)
        status = int(getattr(resp, "status_code", getattr(resp, "status", 0)))
        resp_headers = _header_mapping(resp)
        if status >= 400:
            error_type, message = parse_anthropic_error_payload(data)
            raise AnthropicUpstreamError(
                status,
                error_type or f"http_{status}",
                message or "Anthropic upstream request failed",
                retry_after_seconds=_retry_after_seconds(resp),
            )
        return MessagesResult(status_code=status, headers=resp_headers, data=data)
    if not allow_direct_egress:
        raise ValueError("Anthropic Messages egress requires a resolved route or explicit direct-egress opt-in")
    async with lease_http_session(session) as client_session:
        async with client_session.post(url, json=dict(payload), headers=headers, timeout=timeout) as resp:
            data = await _response_json(resp)
            resp_headers = _header_mapping(resp)
            if resp.status >= 400:
                error_type, message = parse_anthropic_error_payload(data)
                raise AnthropicUpstreamError(
                    resp.status,
                    error_type or f"http_{resp.status}",
                    message or "Anthropic upstream request failed",
                    retry_after_seconds=_retry_after_seconds(resp),
                )
            return MessagesResult(status_code=resp.status, headers=resp_headers, data=data)


async def stream_messages(
    payload: Mapping[str, Any],
    *,
    pool_class: str,
    access_token: str,
    route: ResolvedUpstreamRoute | None = None,
    codex_client: CodexClient | None = None,
    session: aiohttp.ClientSession | None = None,
    allow_direct_egress: bool = False,
) -> AsyncIterator[str]:
    """Streaming Messages call. Yields raw SSE blocks (``event:``/``data:`` lines
    separated by a blank line) in upstream order.

    Raises :class:`AnthropicUpstreamError` before the first yield when the
    upstream answers non-2xx, so callers can turn startup failures into real
    HTTP error responses and fail over before committing to a stream.
    """
    url = _messages_url()
    headers = build_messages_headers(pool_class, access_token)
    headers["accept"] = "text/event-stream"
    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=MESSAGES_STREAM_CONNECT_TIMEOUT_SECONDS,
        sock_read=MESSAGES_STREAM_IDLE_TIMEOUT_SECONDS,
    )

    if route is not None:
        resp = await _codex_request(
            "POST",
            url=url,
            route=route,
            codex_client=codex_client,
            buffer_response=False,
            json=dict(payload),
            headers=headers,
            timeout=timeout,
        )
        status = int(getattr(resp, "status_code", getattr(resp, "status", 0)))
        if status >= 400:
            await _raise_from_response(resp)
        async with _aclose_response(resp):
            async for block in _iter_sse_blocks(resp):
                yield block
        return
    if not allow_direct_egress:
        raise ValueError("Anthropic Messages egress requires a resolved route or explicit direct-egress opt-in")
    async with lease_http_session(session) as client_session:
        async with client_session.post(url, json=dict(payload), headers=headers, timeout=timeout) as resp:
            if resp.status >= 400:
                await _raise_from_response(resp)
            async for block in _iter_sse_blocks(resp):
                yield block


def _header_mapping(resp: Any) -> Mapping[str, str]:
    headers = getattr(resp, "headers", None)
    if headers is None:
        return {}
    try:
        return {str(k): str(v) for k, v in headers.items()}
    except Exception:
        return {}


class _aclose_response:
    """Release a CodexClient-owned streaming response when the consumer leaves."""

    def __init__(self, resp: Any) -> None:
        self._resp = resp

    async def __aenter__(self) -> Any:
        return self._resp

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        release = getattr(self._resp, "release", None)
        if callable(release):
            result = release()
            if hasattr(result, "__await__"):
                await result
            return
        close = getattr(self._resp, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result


async def _iter_sse_blocks(resp: Any) -> AsyncIterator[str]:
    """Split the upstream byte stream into SSE blocks at blank lines."""
    content = getattr(resp, "content", None)
    if content is None:
        return
    iterator = getattr(content, "iter_chunked", None)
    if callable(iterator):
        chunks = iterator(_STREAM_CHUNK_BYTES)
    else:
        iter_any = getattr(content, "iter_any", None)
        if not callable(iter_any):
            return
        chunks = iter_any()
    pending: list[str] = []
    buffer = ""
    async for chunk in chunks:
        buffer += chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
        while "\n" in buffer:
            line, _, buffer = buffer.partition("\n")
            if line.strip() == "":
                if pending:
                    yield "\n".join(pending) + "\n"
                    pending = []
            else:
                pending.append(line)
    if pending:
        yield "\n".join(pending) + "\n"
