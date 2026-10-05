"""Anthropic `/v1/messages` service slice (openspec change anthropic-proxy-core).

Selection, failover, and settlement reuse the existing ProxyService machinery;
this module only adapts the Anthropic wire shapes and the per-pool-class
upstream call. Invariants kept here:

- Reservations settle (finalize or release) BEFORE account error-health writes
  on every path, including mid-stream death and failover.
- A failed account is added to the selection exclusion set before the next
  attempt, so excluded accounts actually leave the loop.
- Client disconnects release the reservation without marking the account
  unhealthy (a disconnect is not account evidence).
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from typing import Any, NoReturn, Protocol, cast

from app.core.anthropic.models import MessagesRequest, parse_anthropic_error_payload
from app.core.auth.refresh import RefreshError
from app.core.balancer import ResetPreferenceWindow, RoutingStrategy
from app.core.clients.anthropic import (
    AnthropicUpstreamError,
    MessagesResult,
)
from app.core.clients.anthropic import (
    request_messages as core_request_messages,
)
from app.core.clients.anthropic import (
    stream_messages as core_stream_messages,
)
from app.core.clients.proxy import ProxyResponseError
from app.core.config.settings import get_settings
from app.core.config.settings_cache import get_settings_cache
from app.core.errors import OpenAIErrorEnvelope, openai_error
from app.core.resilience.toggles import bind_resilience_toggles
from app.core.types import JsonObject
from app.core.upstream_proxy import ResolvedUpstreamRoute, UpstreamProxyRouteError
from app.core.utils.request_id import ensure_request_id, get_request_id
from app.db.models import Account
from app.modules.api_keys.service import ApiKeyData, ApiKeyUsageReservationData
from app.modules.proxy._service.support import _request_log_client_fields, _RequestLogFailureMetadata
from app.modules.proxy.helpers import _normalize_error_code
from app.modules.proxy.load_balancer import AccountSelection
from app.modules.proxy.selection_errors import selection_failure_response

logger = logging.getLogger("app.modules.proxy.service")

_REQUEST_TRANSPORT_HTTP = "anthropic_messages"
_MESSAGES_MAX_ACCOUNT_ATTEMPTS = 3
_MESSAGES_OPERATION = "messages"

# Anthropic error types that describe the request, not the account: they must
# never write account health (house rule: account-neutral rejections are
# skipped).
_CLIENT_SHAPED_ERROR_TYPES = frozenset({"invalid_request_error", "not_found_error"})

# Failover-eligible upstream statuses for a Messages attempt.
_FAILOVER_ELIGIBLE_STATUSES = frozenset({401, 429, 500, 502, 503, 504, 529})


class _AnthropicServiceProtocol(Protocol):
    _repo_factory: Any
    _encryptor: Any
    _load_balancer: Any
    _clock: Any

    async def _select_account_with_budget(self, deadline: float, **kwargs: Any) -> AccountSelection: ...
    async def _ensure_fresh_with_budget_or_auth_error(
        self, account: Account, *, timeout_seconds: float, force: bool = False
    ) -> Account: ...
    async def _handle_proxy_error(self, account: Account, exc: ProxyResponseError) -> None: ...
    async def _resolve_upstream_route_for_account(
        self, account: Account, *, operation: str
    ) -> ResolvedUpstreamRoute | None: ...
    async def _write_request_log(self, **kwargs: Any) -> None: ...
    async def _settle_stream_api_key_usage(
        self,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        settlement: Any,
        request_id: str,
    ) -> bool: ...


def _service_global(name: str) -> Any:
    service_module = sys.modules.get("app.modules.proxy.service")
    if service_module is None:
        raise RuntimeError("app.modules.proxy.service is not loaded")
    return getattr(service_module, name)


def _service_core_stream_messages() -> Callable[..., Any]:
    service_module = sys.modules.get("app.modules.proxy.service")
    if service_module is not None:
        return cast(
            Callable[..., Any],
            getattr(service_module, "core_stream_messages", core_stream_messages),
        )
    return core_stream_messages


def _service_core_request_messages() -> Callable[..., Any]:
    service_module = sys.modules.get("app.modules.proxy.service")
    if service_module is not None:
        return cast(
            Callable[..., Any],
            getattr(service_module, "core_request_messages", core_request_messages),
        )
    return core_request_messages


def _service_global_or(name: str, fallback: Any) -> Any:
    service_module = sys.modules.get("app.modules.proxy.service")
    if service_module is None:
        return fallback
    return getattr(service_module, name, fallback)


def _service_get_settings() -> Any:
    return _service_global_or("get_settings", get_settings)()


def _service_get_settings_cache() -> Any:
    return _service_global_or("get_settings_cache", get_settings_cache)()


def _remaining_budget_seconds(deadline: float) -> float:
    return cast(Callable[[float], float], _service_global("_remaining_budget_seconds"))(deadline)


def _raise_proxy_budget_exhausted() -> NoReturn:
    cast(Callable[[], NoReturn], _service_global("_raise_proxy_budget_exhausted"))()


def _request_log_failure_metadata(exc: ProxyResponseError) -> _RequestLogFailureMetadata:
    return cast(
        Callable[[ProxyResponseError], _RequestLogFailureMetadata], _service_global("_request_log_failure_metadata")
    )(exc)


def _stream_settlement(**kwargs: Any) -> Any:
    return cast(Callable[..., Any], _service_global("_StreamSettlement"))(**kwargs)


def _routing_strategy(settings: Any) -> RoutingStrategy:
    return cast(Callable[[Any], RoutingStrategy], _service_global("_routing_strategy"))(settings)


def _prefer_earlier_reset_window(settings: Any) -> ResetPreferenceWindow:
    return cast(Callable[[Any], ResetPreferenceWindow], _service_global("_prefer_earlier_reset_window"))(settings)


def _proxy_response_failed_account(exc: ProxyResponseError, fallback: Account) -> Account:
    return cast(Callable[[ProxyResponseError, Account], Account], _service_global("_proxy_response_failed_account"))(
        exc, fallback
    )


def _anthropic_upstream_error(exc: AnthropicUpstreamError) -> ProxyResponseError:
    """Map an Anthropic upstream failure onto the ProxyResponseError machinery.

    The payload keeps the Anthropic envelope (it is also the downstream body);
    ``error.type`` normalizes through the existing code path. The classifier's
    rate-limit/transient sets recognize Anthropic's ``rate_limit_error`` and
    ``api_error`` types.
    """
    return ProxyResponseError(
        exc.status_code,
        cast(
            OpenAIErrorEnvelope,
            {"type": "error", "error": {"type": exc.error_type, "message": exc.message}},
        ),
        failure_phase="request" if exc.status_code >= 400 else "connect",
        upstream_status_code=exc.status_code,
        upstream_error_code=exc.error_type,
        retry_after_seconds=exc.retry_after_seconds,
    )


def _client_shaped_error(exc: ProxyResponseError) -> bool:
    """True when the failure describes the request, not the account."""
    if exc.status_code == 400:
        return True
    error_type, _ = parse_anthropic_error_payload(exc.payload)
    return error_type in _CLIENT_SHAPED_ERROR_TYPES


def _failover_eligible(exc: ProxyResponseError) -> bool:
    if exc.status_code in _FAILOVER_ELIGIBLE_STATUSES:
        return True
    if exc.failure_phase == "connect":
        return True
    error_type, _ = parse_anthropic_error_payload(exc.payload)
    return error_type in {"overloaded_error", "api_error", "rate_limit_error"}


def _usage_from_sse_block(block: str) -> tuple[int | None, int | None, int | None]:
    """Extract ``(input_tokens, output_tokens, cache_read_input_tokens)`` from
    Anthropic SSE blocks that carry a usage object (message_start/message_delta)."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read: int | None = None
    for line in block.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[len("data:") :].strip())
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        usage = data.get("usage")
        if not isinstance(usage, dict):
            continue
        candidate_in = usage.get("input_tokens")
        candidate_out = usage.get("output_tokens")
        candidate_cache = usage.get("cache_read_input_tokens")
        if isinstance(candidate_in, int):
            input_tokens = candidate_in
        if isinstance(candidate_out, int):
            output_tokens = candidate_out
        if isinstance(candidate_cache, int):
            cache_read = candidate_cache
    return input_tokens, output_tokens, cache_read


def _record_rate_limit_headers(account: Account, headers: Mapping[str, str]) -> None:
    """Evidence capture for umbrella task 3.3 (parked live verification).

    Values are logged at debug until the window-constants change maps them onto
    the usage schema; no capacity constants are invented here.
    """
    observed = {
        name: value
        for name, value in headers.items()
        if name.lower().startswith(("anthropic-ratelimit-", "anthropic-priority-", "retry-after"))
    }
    if observed:
        logger.debug(
            "Anthropic rate-limit headers observed account_id=%s headers=%s",
            account.id,
            observed,
        )


class _AnthropicMixin:
    async def messages_unary(
        self,
        *,
        payload: MessagesRequest,
        api_key: ApiKeyData | None = None,
        api_key_reservation: ApiKeyUsageReservationData | None = None,
        headers: Mapping[str, str],
    ) -> tuple[JsonObject, int]:
        """Serve one non-streaming `/v1/messages` request. Returns (body, status)."""
        proxy = cast(_AnthropicServiceProtocol, self)
        useragent, useragent_group, conversation_id = _request_log_client_fields(headers)
        request_id = get_request_id() or ensure_request_id(None)
        start = proxy._clock.monotonic()
        base_settings = _service_get_settings()
        deadline = start + base_settings.proxy_request_budget_seconds
        settings = await _service_get_settings_cache().get()
        bind_resilience_toggles(settings)
        prefer_earlier_reset = settings.prefer_earlier_reset_accounts
        routing_strategy = _routing_strategy(settings)
        payload_dict = payload.to_payload()
        account_id_value: str | None = None
        log_status = "error"
        log_error_code: str | None = None
        log_error_message: str | None = None
        failure_metadata = _RequestLogFailureMetadata()

        async def _call(target: Account) -> MessagesResult:
            remaining_budget = _remaining_budget_seconds(deadline)
            if remaining_budget <= 0:
                _raise_proxy_budget_exhausted()
            route = await proxy._resolve_upstream_route_for_account(target, operation=_MESSAGES_OPERATION)
            access_token = proxy._encryptor.decrypt(target.access_token_encrypted)
            try:
                return await _service_core_request_messages()(
                    payload_dict,
                    pool_class=target.pool_class or "oauth_seat",
                    access_token=access_token,
                    route=route,
                    allow_direct_egress=route is None,
                )
            except AnthropicUpstreamError as upstream_exc:
                raise _anthropic_upstream_error(upstream_exc) from upstream_exc

        try:
            excluded: set[str] = set()
            force_refreshed: set[str] = set()
            last_error: ProxyResponseError | None = None
            for _attempt in range(_MESSAGES_MAX_ACCOUNT_ATTEMPTS):
                selection = await proxy._select_account_with_budget(
                    deadline,
                    request_id=request_id,
                    kind=_MESSAGES_OPERATION,
                    api_key=api_key,
                    prefer_earlier_reset_accounts=prefer_earlier_reset,
                    prefer_earlier_reset_window=_prefer_earlier_reset_window(settings),
                    routing_strategy=routing_strategy,
                    model=payload.model,
                    exclude_account_ids=excluded,
                )
                account = selection.account
                if not account:
                    log_error_code = selection.error_code or "no_accounts"
                    log_error_message = selection.error_message or "No active accounts available"
                    status_code, error_payload = selection_failure_response(selection)
                    if last_error is not None and status_code >= 500:
                        raise last_error
                    raise ProxyResponseError(status_code, error_payload)
                account_id_value = account.id
                try:
                    account = await proxy._ensure_fresh_with_budget_or_auth_error(
                        account,
                        timeout_seconds=_remaining_budget_seconds(deadline),
                    )
                    account_id_value = account.id
                    result = await _call(account)
                except ProxyResponseError as exc:
                    failed_account = _proxy_response_failed_account(exc, account)
                    account_id_value = failed_account.id
                    # Settlement first: the reservation must leave the ledger
                    # before this account's health is written.
                    await proxy._settle_stream_api_key_usage(
                        api_key,
                        api_key_reservation,
                        _stream_settlement(
                            status="failed",
                            model=payload.model,
                            error_code=str(exc.upstream_error_code or exc.status_code),
                            record_success=False,
                        ),
                        request_id,
                    )
                    if not _client_shaped_error(exc):
                        await proxy._handle_proxy_error(failed_account, exc)
                    if (
                        exc.status_code == 401
                        and failed_account.id not in force_refreshed
                        and _remaining_budget_seconds(deadline) > 0
                    ):
                        force_refreshed.add(failed_account.id)
                        excluded.discard(failed_account.id)
                        last_error = exc
                        continue
                    if _failover_eligible(exc):
                        excluded.add(failed_account.id)
                        last_error = exc
                        continue
                    raise
                except RefreshError as refresh_exc:
                    failed_account = account
                    await proxy._settle_stream_api_key_usage(
                        api_key,
                        api_key_reservation,
                        _stream_settlement(status="failed", model=payload.model, record_success=False),
                        request_id,
                    )
                    if refresh_exc.is_permanent:
                        await proxy._load_balancer.mark_permanent_failure(failed_account, refresh_exc.code)
                    raise ProxyResponseError(
                        401,
                        openai_error(
                            "authentication_error",
                            refresh_exc.message,
                            error_type="authentication_error",
                        ),
                    ) from refresh_exc
                _record_rate_limit_headers(account, result.headers)
                await proxy._settle_stream_api_key_usage(
                    api_key,
                    api_key_reservation,
                    _stream_settlement(
                        status="success",
                        model=payload.model,
                        input_tokens=result.data.get("usage", {}).get("input_tokens")
                        if isinstance(result.data.get("usage"), dict)
                        else None,
                        output_tokens=result.data.get("usage", {}).get("output_tokens")
                        if isinstance(result.data.get("usage"), dict)
                        else None,
                    ),
                    request_id,
                )
                await proxy._load_balancer.record_success(account)
                log_status = "success"
                return result.data, result.status_code
            raise last_error or ProxyResponseError(
                503,
                openai_error("no_accounts", "No active accounts available"),
            )
        except ProxyResponseError as exc:
            failed_account = getattr(exc, "_claude_lb_failed_account", None)
            if isinstance(failed_account, Account):
                account_id_value = failed_account.id
            failure_metadata = _request_log_failure_metadata(exc)
            error_type, message = parse_anthropic_error_payload(exc.payload)
            log_error_code = log_error_code or _normalize_error_code(None, error_type)
            log_error_message = log_error_message or message
            raise
        except UpstreamProxyRouteError as exc:
            log_error_code = "upstream_proxy_unavailable"
            log_error_message = exc.reason
            raise ProxyResponseError(
                502,
                openai_error("upstream_proxy_unavailable", f"Upstream proxy route unavailable: {exc.reason}"),
            ) from exc
        finally:
            await proxy._write_request_log(
                account_id=account_id_value,
                api_key=api_key,
                request_id=request_id,
                model=payload.model,
                latency_ms=int((proxy._clock.monotonic() - start) * 1000),
                status=log_status,
                error_code=log_error_code,
                error_message=log_error_message,
                transport=_REQUEST_TRANSPORT_HTTP,
                failure_phase=failure_metadata.failure_phase,
                failure_detail=failure_metadata.failure_detail,
                failure_exception_type=failure_metadata.failure_exception_type,
                upstream_status_code=failure_metadata.upstream_status_code,
                upstream_error_code=failure_metadata.upstream_error_code,
                bridge_stage=None,
                upstream_proxy_route_mode=None,
                upstream_proxy_pool_id=None,
                upstream_proxy_endpoint_id=None,
                upstream_proxy_fallback_used=None,
                upstream_proxy_fail_closed_reason=None,
                useragent=useragent,
                useragent_group=useragent_group,
                conversation_id=conversation_id,
            )

    def stream_messages_request(
        self,
        *,
        payload: MessagesRequest,
        api_key: ApiKeyData | None = None,
        api_key_reservation: ApiKeyUsageReservationData | None = None,
        headers: Mapping[str, str],
    ) -> AsyncGenerator[str, None]:
        """Serve one streaming `/v1/messages` request as raw SSE blocks.

        The first block surfaces only after an account is committed to; startup
        failures (before any upstream event) raise before the first yield so the
        route can answer with a real HTTP error.
        """
        return self._stream_messages_events(
            payload=payload,
            api_key=api_key,
            api_key_reservation=api_key_reservation,
            headers=headers,
        )

    async def _stream_messages_events(
        self,
        *,
        payload: MessagesRequest,
        api_key: ApiKeyData | None,
        api_key_reservation: ApiKeyUsageReservationData | None,
        headers: Mapping[str, str],
    ) -> AsyncGenerator[str, None]:
        proxy = cast(_AnthropicServiceProtocol, self)
        useragent, useragent_group, conversation_id = _request_log_client_fields(headers)
        request_id = get_request_id() or ensure_request_id(None)
        start = proxy._clock.monotonic()
        base_settings = _service_get_settings()
        deadline = start + base_settings.proxy_request_budget_seconds
        settings = await _service_get_settings_cache().get()
        bind_resilience_toggles(settings)
        prefer_earlier_reset = settings.prefer_earlier_reset_accounts
        routing_strategy = _routing_strategy(settings)
        payload_dict = payload.to_payload()
        account_id_value: str | None = None
        log_status = "error"
        log_error_code: str | None = None
        log_error_message: str | None = None
        failure_metadata = _RequestLogFailureMetadata()

        async def _open_stream(target: Account) -> AsyncIterator[str]:
            route = await proxy._resolve_upstream_route_for_account(target, operation=_MESSAGES_OPERATION)
            access_token = proxy._encryptor.decrypt(target.access_token_encrypted)
            return _service_core_stream_messages()(
                payload_dict,
                pool_class=target.pool_class or "oauth_seat",
                access_token=access_token,
                route=route,
                allow_direct_egress=route is None,
            )

        async def _first_block(target: Account) -> tuple[AsyncIterator[str], str]:
            """Open the upstream stream and surface its first SSE block.

            A non-2xx upstream answer raises before the first yield, so the
            startup path can treat it exactly like any other attempt failure."""
            upstream = await _open_stream(target)
            try:
                first = await upstream.__anext__()
            except AnthropicUpstreamError as upstream_exc:
                raise _anthropic_upstream_error(upstream_exc) from upstream_exc
            return upstream, first

        async def _settle_failed() -> None:
            await proxy._settle_stream_api_key_usage(
                api_key,
                api_key_reservation,
                _stream_settlement(status="failed", model=payload.model, record_success=False),
                request_id,
            )

        try:
            excluded: set[str] = set()
            force_refreshed: set[str] = set()
            last_error: ProxyResponseError | None = None
            for _attempt in range(_MESSAGES_MAX_ACCOUNT_ATTEMPTS):
                selection = await proxy._select_account_with_budget(
                    deadline,
                    request_id=request_id,
                    kind=_MESSAGES_OPERATION,
                    api_key=api_key,
                    prefer_earlier_reset_accounts=prefer_earlier_reset,
                    prefer_earlier_reset_window=_prefer_earlier_reset_window(settings),
                    routing_strategy=routing_strategy,
                    model=payload.model,
                    exclude_account_ids=excluded,
                )
                account = selection.account
                if not account:
                    log_error_code = selection.error_code or "no_accounts"
                    log_error_message = selection.error_message or "No active accounts available"
                    status_code, error_payload = selection_failure_response(selection)
                    if last_error is not None and status_code >= 500:
                        raise last_error
                    raise ProxyResponseError(status_code, error_payload)
                account_id_value = account.id
                try:
                    account = await proxy._ensure_fresh_with_budget_or_auth_error(
                        account,
                        timeout_seconds=_remaining_budget_seconds(deadline),
                    )
                    account_id_value = account.id
                    upstream, first_block = await _first_block(account)
                except StopAsyncIteration:
                    # Upstream closed without emitting anything: treat as a
                    # transient upstream failure on this account.
                    exc = ProxyResponseError(502, openai_error("api_error", "Upstream stream closed empty"))
                    await _settle_failed()
                    await proxy._handle_proxy_error(account, exc)
                    excluded.add(account.id)
                    last_error = exc
                    continue
                except ProxyResponseError as exc:
                    failed_account = _proxy_response_failed_account(exc, account)
                    account_id_value = failed_account.id
                    await _settle_failed()
                    if not _client_shaped_error(exc):
                        await proxy._handle_proxy_error(failed_account, exc)
                    if (
                        exc.status_code == 401
                        and failed_account.id not in force_refreshed
                        and _remaining_budget_seconds(deadline) > 0
                    ):
                        force_refreshed.add(failed_account.id)
                        last_error = exc
                        continue
                    if _failover_eligible(exc):
                        excluded.add(failed_account.id)
                        last_error = exc
                        continue
                    raise
                except RefreshError as refresh_exc:
                    await _settle_failed()
                    if refresh_exc.is_permanent:
                        await proxy._load_balancer.mark_permanent_failure(account, refresh_exc.code)
                    raise ProxyResponseError(
                        401,
                        openai_error(
                            "authentication_error",
                            refresh_exc.message,
                            error_type="authentication_error",
                        ),
                    ) from refresh_exc

                # Committed to this account's stream: order and terminations
                # pass through untouched.
                input_tokens: int | None = None
                output_tokens: int | None = None
                try:
                    block = first_block
                    while True:
                        new_in, new_out, _ = _usage_from_sse_block(block)
                        input_tokens = new_in if new_in is not None else input_tokens
                        output_tokens = new_out if new_out is not None else output_tokens
                        yield block
                        block = await upstream.__anext__()
                except StopAsyncIteration:
                    await proxy._settle_stream_api_key_usage(
                        api_key,
                        api_key_reservation,
                        _stream_settlement(
                            status="success",
                            model=payload.model,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        ),
                        request_id,
                    )
                    await proxy._load_balancer.record_success(account)
                    log_status = "success"
                    return
                except GeneratorExit:
                    # Client went away: release the reservation, never punish
                    # the account for a downstream disconnect.
                    await proxy._settle_stream_api_key_usage(
                        api_key,
                        api_key_reservation,
                        _stream_settlement(status="failed", model=payload.model, record_success=False),
                        request_id,
                    )
                    raise
                except AnthropicUpstreamError as upstream_exc:
                    exc = _anthropic_upstream_error(upstream_exc)
                    await _settle_failed()
                    await proxy._handle_proxy_error(account, exc)
                    raise exc
                except ProxyResponseError as exc:
                    await _settle_failed()
                    await proxy._handle_proxy_error(account, exc)
                    raise
            raise last_error or ProxyResponseError(
                503,
                openai_error("no_accounts", "No active accounts available"),
            )
        except ProxyResponseError as exc:
            failed_account = getattr(exc, "_claude_lb_failed_account", None)
            if isinstance(failed_account, Account):
                account_id_value = failed_account.id
            failure_metadata = _request_log_failure_metadata(exc)
            error_type, message = parse_anthropic_error_payload(exc.payload)
            log_error_code = log_error_code or _normalize_error_code(None, error_type)
            log_error_message = log_error_message or message
            raise
        except UpstreamProxyRouteError as exc:
            log_error_code = "upstream_proxy_unavailable"
            log_error_message = exc.reason
            raise ProxyResponseError(
                502,
                openai_error("upstream_proxy_unavailable", f"Upstream proxy route unavailable: {exc.reason}"),
            ) from exc
        finally:
            await proxy._write_request_log(
                account_id=account_id_value,
                api_key=api_key,
                request_id=request_id,
                model=payload.model,
                latency_ms=int((proxy._clock.monotonic() - start) * 1000),
                status=log_status,
                error_code=log_error_code,
                error_message=log_error_message,
                transport=_REQUEST_TRANSPORT_HTTP,
                failure_phase=failure_metadata.failure_phase,
                failure_detail=failure_metadata.failure_detail,
                failure_exception_type=failure_metadata.failure_exception_type,
                upstream_status_code=failure_metadata.upstream_status_code,
                upstream_error_code=failure_metadata.upstream_error_code,
                bridge_stage=None,
                upstream_proxy_route_mode=None,
                upstream_proxy_pool_id=None,
                upstream_proxy_endpoint_id=None,
                upstream_proxy_fallback_used=None,
                upstream_proxy_fail_closed_reason=None,
                useragent=useragent,
                useragent_group=useragent_group,
                conversation_id=conversation_id,
            )
