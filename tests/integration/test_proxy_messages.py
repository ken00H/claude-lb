"""Route-layer tests for the Anthropic `/v1/messages` proxy surface
(openspec change anthropic-proxy-core). The Anthropic upstream client is
monkeypatched through the service slice; everything else runs the real app."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select

import app.modules.proxy._service.anthropic as anthropic_service_module
import app.modules.proxy.service as proxy_module
from app.core.clients.anthropic import AnthropicUpstreamError, MessagesResult
from app.core.crypto import TokenEncryptor
from app.core.utils.time import utcnow
from app.db.models import Account, AccountStatus
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


async def _seed_oauth_seat(
    slug: str,
    email: str,
    *,
    plan_type: str = "free",
    pool_class: str = "oauth_seat",
) -> str:
    encryptor = TokenEncryptor()
    account_id = f"anth-{slug}"
    async with SessionLocal() as session:
        session.add(
            Account(
                id=account_id,
                pool_class=pool_class,
                anthropic_account_id=slug,
                anthropic_organization_id=f"org-{slug}",
                email=email,
                plan_type=plan_type,
                access_token_encrypted=encryptor.encrypt(f"access-{account_id}"),
                refresh_token_encrypted=encryptor.encrypt(f"refresh-{account_id}"),
                last_refresh=utcnow(),
                status=AccountStatus.ACTIVE,
            )
        )
        await session.commit()
    return account_id


async def _seed_api_key(slug: str) -> str:
    return await _seed_oauth_seat(slug, f"{slug}@apikeys.local", plan_type="console", pool_class="api_key")


async def _account_status(account_id: str) -> AccountStatus | None:
    async with SessionLocal() as session:
        account = await session.get(Account, account_id)
        return account.status if account is not None else None


async def _all_account_ids() -> set[str]:
    async with SessionLocal() as session:
        return {account.id for account in (await session.execute(select(Account))).scalars().all()}


def _messages_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }
    payload.update(overrides)
    return payload


def _messages_body() -> dict[str, Any]:
    return {
        "id": "msg_test_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": "hello"}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }


def _sse_blocks() -> list[str]:
    return [
        'event: message_start\ndata: {"type": "message_start", "message": {"usage": {"input_tokens": 3}}}\n\n',
        'event: content_block_delta\ndata: {"type": "content_block_delta", "delta": {"text": "hello"}}\n\n',
        'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]


@pytest.mark.asyncio
async def test_messages_non_streaming_round_trip(async_client, monkeypatch):
    await _seed_oauth_seat("msgs_ok", "msgs-ok@example.com")
    seen: dict[str, Any] = {}

    async def fake_request_messages(payload, *, pool_class, access_token, route=None, **kwargs):
        del kwargs
        seen["payload"] = payload
        seen["pool_class"] = pool_class
        seen["access_token"] = access_token
        return MessagesResult(
            status_code=200,
            headers={"anthropic-ratelimit-requests-limit": "50"},
            data=_messages_body(),
        )

    monkeypatch.setattr(anthropic_service_module, "core_request_messages", fake_request_messages)

    response = await async_client.post("/v1/messages", json=_messages_payload())

    assert response.status_code == 200
    assert response.json() == _messages_body()
    assert response.headers.get("request-id")
    assert seen["pool_class"] == "oauth_seat"
    assert seen["access_token"] == "access-anth-msgs_ok"
    assert seen["payload"]["model"] == "claude-sonnet-4-5"
    assert seen["payload"]["max_tokens"] == 64


@pytest.mark.asyncio
async def test_messages_streaming_preserves_event_order(async_client, monkeypatch):
    await _seed_oauth_seat("msgs_stream", "msgs-stream@example.com")

    async def fake_stream_messages(payload, *, pool_class, access_token, route=None, **kwargs):
        del payload, pool_class, access_token, route, kwargs
        for block in _sse_blocks():
            yield block

    monkeypatch.setattr(anthropic_service_module, "core_stream_messages", fake_stream_messages)

    async with async_client.stream("POST", "/v1/messages", json=_messages_payload(stream=True)) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = await response.aread()

    assert body.decode("utf-8") == "".join(_sse_blocks())


@pytest.mark.asyncio
async def test_messages_invalid_payload_rejected_before_account_selection(async_client, monkeypatch):
    called = False

    async def fake_request_messages(payload, **kwargs):
        nonlocal called
        called = True
        del payload, kwargs
        raise AssertionError("upstream must not be called for an invalid payload")

    monkeypatch.setattr(anthropic_service_module, "core_request_messages", fake_request_messages)

    response = await async_client.post(
        "/v1/messages",
        json={"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert called is False


@pytest.mark.asyncio
async def test_messages_no_accounts_surfaces_anthropic_envelope(async_client):
    response = await async_client.post("/v1/messages", json=_messages_payload())

    assert response.status_code == 503
    body = response.json()
    assert body["type"] == "error"
    assert isinstance(body["error"], dict)


@pytest.mark.asyncio
async def test_messages_rate_limit_fails_over_and_marks_account_rate_limited(async_client, monkeypatch):
    await _seed_oauth_seat("msgs_rl_a", "msgs-rl-a@example.com")
    await _seed_oauth_seat("msgs_rl_b", "msgs-rl-b@example.com")
    requested_accounts: list[str] = []

    async def fake_request_messages(payload, *, pool_class, access_token, route=None, **kwargs):
        del payload, pool_class, route, kwargs
        requested_accounts.append(access_token)
        if len(requested_accounts) == 1:
            raise AnthropicUpstreamError(429, "rate_limit_error", "rate limited", retry_after_seconds=7)
        return MessagesResult(status_code=200, headers={}, data=_messages_body())

    monkeypatch.setattr(anthropic_service_module, "core_request_messages", fake_request_messages)

    response = await async_client.post("/v1/messages", json=_messages_payload())

    assert response.status_code == 200
    assert response.json() == _messages_body()
    assert len(requested_accounts) == 2
    assert len(set(requested_accounts)) == 2
    failed_account_id = requested_accounts[0].removeprefix("access-")
    assert await _account_status(failed_account_id) == AccountStatus.RATE_LIMITED
    assert requested_accounts[1].removeprefix("access-") != failed_account_id


@pytest.mark.asyncio
async def test_messages_upstream_500_fails_over_to_next_account(async_client, monkeypatch):
    await _seed_oauth_seat("msgs_5xx_a", "msgs-5xx-a@example.com")
    await _seed_oauth_seat("msgs_5xx_b", "msgs-5xx-b@example.com")
    requested_accounts: list[str] = []

    async def fake_request_messages(payload, *, pool_class, access_token, route=None, **kwargs):
        del payload, pool_class, route, kwargs
        requested_accounts.append(access_token)
        if len(requested_accounts) == 1:
            raise AnthropicUpstreamError(500, "api_error", "boom")
        return MessagesResult(status_code=200, headers={}, data=_messages_body())

    monkeypatch.setattr(anthropic_service_module, "core_request_messages", fake_request_messages)

    response = await async_client.post("/v1/messages", json=_messages_payload())

    assert response.status_code == 200
    assert len(requested_accounts) == 2
    assert len(set(requested_accounts)) == 2


@pytest.mark.asyncio
async def test_messages_api_key_pool_uses_its_own_credential(async_client, monkeypatch):
    key_id = await _seed_api_key("msgs_apikey")
    seen: dict[str, Any] = {}

    async def fake_request_messages(payload, *, pool_class, access_token, route=None, **kwargs):
        del payload, route, kwargs
        seen["pool_class"] = pool_class
        seen["access_token"] = access_token
        return MessagesResult(status_code=200, headers={}, data=_messages_body())

    monkeypatch.setattr(anthropic_service_module, "core_request_messages", fake_request_messages)

    response = await async_client.post("/v1/messages", json=_messages_payload())

    assert response.status_code == 200
    assert seen["pool_class"] == "api_key"
    assert seen["access_token"] == f"access-{key_id}"


@pytest.mark.asyncio
async def test_messages_mixed_pool_serves_both_classes(async_client, monkeypatch):
    """Failover across the pool must cross the class boundary: the first account
    (whichever class selection picks) fails, the second must be the other class."""
    await _seed_oauth_seat("msgs_mixed_seat", "msgs-mixed-seat@example.com")
    await _seed_api_key("msgs_mixed_key")
    requested_classes: list[str] = []

    async def fake_request_messages(payload, *, pool_class, access_token, route=None, **kwargs):
        del payload, access_token, route, kwargs
        requested_classes.append(pool_class)
        if len(requested_classes) == 1:
            raise AnthropicUpstreamError(429, "rate_limit_error", "rate limited")
        return MessagesResult(status_code=200, headers={}, data=_messages_body())

    monkeypatch.setattr(anthropic_service_module, "core_request_messages", fake_request_messages)

    response = await async_client.post("/v1/messages", json=_messages_payload())

    assert response.status_code == 200
    assert requested_classes[0] in {"oauth_seat", "api_key"}
    assert requested_classes[1] != requested_classes[0]


@pytest.mark.asyncio
async def test_messages_unknown_plan_account_routable(async_client, monkeypatch):
    await _seed_oauth_seat("msgs_unknown", "msgs-unknown@example.com", plan_type="unknown")

    async def fake_request_messages(payload, *, pool_class, access_token, route=None, **kwargs):
        del payload, pool_class, route, kwargs
        return MessagesResult(status_code=200, headers={}, data=_messages_body())

    monkeypatch.setattr(anthropic_service_module, "core_request_messages", fake_request_messages)

    response = await async_client.post("/v1/messages", json=_messages_payload())

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_messages_stream_startup_error_is_http_response(async_client, monkeypatch):
    await _seed_oauth_seat("msgs_startup", "msgs-startup@example.com")

    async def fake_stream_messages(payload, *, pool_class, access_token, route=None, **kwargs):
        del payload, pool_class, access_token, route, kwargs
        raise AnthropicUpstreamError(429, "rate_limit_error", "slow down")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(anthropic_service_module, "core_stream_messages", fake_stream_messages)

    response = await async_client.post("/v1/messages", json=_messages_payload(stream=True))

    assert response.status_code == 429
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "rate_limit_error"


@pytest.mark.asyncio
async def test_messages_stream_midstream_failure_yields_anthropic_error_event(async_client, monkeypatch):
    await _seed_oauth_seat("msgs_mid", "msgs-mid@example.com")

    async def fake_stream_messages(payload, *, pool_class, access_token, route=None, **kwargs):
        del payload, pool_class, access_token, route, kwargs
        yield _sse_blocks()[0]
        raise AnthropicUpstreamError(529, "overloaded_error", "overloaded")

    monkeypatch.setattr(anthropic_service_module, "core_stream_messages", fake_stream_messages)

    async with async_client.stream("POST", "/v1/messages", json=_messages_payload(stream=True)) as response:
        assert response.status_code == 200
        body = await response.aread()

    text = body.decode("utf-8")
    assert '"message_start"' in text
    assert "event: error" in text
    assert '"api_error"' in text


@pytest.mark.asyncio
async def test_messages_reservation_settles_before_health_write(async_client, monkeypatch):
    """Upstream 429 at startup: settlement must be recorded before the account
    health write (ordering invariant)."""
    from app.modules.proxy._service.support import _StreamSettlement

    await _seed_oauth_seat("msgs_order", "msgs-order@example.com")
    events: list[str] = []
    real_settle = proxy_module.ProxyService._settle_stream_api_key_usage

    async def spy_settle(self, api_key, reservation, settlement, request_id, *args, **kwargs):
        del settlement
        events.append("settle")
        return await real_settle(self, api_key, reservation, _StreamSettlement(status="failed"), request_id)

    async def spy_health(self, account, exc):
        del self, account, exc
        events.append("health")

    async def fake_stream_messages(payload, *, pool_class, access_token, route=None, **kwargs):
        del payload, pool_class, access_token, route, kwargs
        raise AnthropicUpstreamError(429, "rate_limit_error", "exhausted")
        yield  # pragma: no cover

    monkeypatch.setattr(anthropic_service_module, "core_stream_messages", fake_stream_messages)
    monkeypatch.setattr(proxy_module.ProxyService, "_settle_stream_api_key_usage", spy_settle)
    monkeypatch.setattr(proxy_module.ProxyService, "_handle_proxy_error", spy_health)

    response = await async_client.post("/v1/messages", json=_messages_payload(stream=True))

    assert response.status_code == 429
    assert events[:2] == ["settle", "health"]


def test_messages_stream_blocks_split_on_blank_lines():
    from app.core.clients.anthropic import _iter_sse_blocks

    class _Content:
        async def iter_chunked(self, size):
            del size
            yield b'event: message_start\ndata: {"a": 1}\n\nevent: message_stop\ndata: {"b": 2}\n\n'

    class _Resp:
        content = _Content()

    async def _collect() -> list[str]:
        return [block async for block in _iter_sse_blocks(_Resp())]

    blocks = asyncio.run(_collect())
    assert blocks == [
        'event: message_start\ndata: {"a": 1}\n',
        'event: message_stop\ndata: {"b": 2}\n',
    ]
