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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("unary", (15, 2, 7)),
        ("stream", (15, 2, 7)),
        ("failover", (15, 2, 7)),
        ("partial_error", (15, 2, 7)),
        ("error_event", (15, 2, 7)),
        ("unconfirmed", (15, 2, 7)),
        ("unknown", (None, None, None)),
        ("invalid", (None, None, None)),
        ("zero", (0, 0, 0)),
        ("cache_only", (12, 2, 7)),
    ],
)
async def test_messages_usage_reaches_limits_logs_and_rollups(async_client, app_instance, monkeypatch, mode, expected):
    import json
    from datetime import timedelta

    from app.db.models import ApiKeyLimit, ApiKeyUsageReservation, RequestLog
    from app.modules.accounts.usage_time_rollup import FOLD_LAG, run_hourly_fold_pass
    from app.modules.request_logs.repository import RequestLogsRepository

    await _seed_oauth_seat("usage_a", "usage-a@example.com")
    if mode == "failover":
        await _seed_oauth_seat("usage_b", "usage-b@example.com")
    enabled = await async_client.put("/api/settings", json={"apiKeyAuthEnabled": True})
    assert enabled.status_code == 200
    created = await async_client.post(
        "/api/api-keys/",
        json={
            "name": "usage-test",
            "limits": [{"limitType": "total_tokens", "limitWindow": "weekly", "maxValue": 1000}],
        },
    )
    assert created.status_code == 200
    key = created.json()
    usage = {"input_tokens": 3, "output_tokens": 0, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 7}
    calls = 0
    closed = False
    health_states: list[str] = []
    real_health = proxy_module.ProxyService._handle_proxy_error

    async def check_settlement_before_health(self, account, exc):
        async with SessionLocal() as session:
            states = list(
                (
                    await session.execute(
                        select(ApiKeyUsageReservation.status).where(ApiKeyUsageReservation.api_key_id == key["id"])
                    )
                ).scalars()
            )
            assert states
            assert not any(state in {"reserved", "settling"} for state in states)
            health_states.extend(states)
        await real_health(self, account, exc)

    monkeypatch.setattr(proxy_module.ProxyService, "_handle_proxy_error", check_settlement_before_health)
    if mode == "unconfirmed":
        real_settle = proxy_module.ProxyService._settle_stream_api_key_usage
        unconfirmed = True

        async def settle_without_first_confirmation(self, *args, **kwargs):
            nonlocal unconfirmed
            result = await real_settle(self, *args, **kwargs)
            if unconfirmed:
                unconfirmed = False
                return False
            return result

        monkeypatch.setattr(
            proxy_module.ProxyService, "_settle_stream_api_key_usage", settle_without_first_confirmation
        )
    blocks = [
        "event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {"usage": usage}}) + "\n\n",
        'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":1}}\n\n',
        'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":2}}\n\n',
        'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]

    async def fake_request(*args, **kwargs):
        nonlocal calls
        calls += 1
        if mode == "failover" and calls == 1:
            raise AnthropicUpstreamError(500, "api_error", "retry")
        body = _messages_body()
        if mode == "unknown":
            body.pop("usage")
        elif mode == "invalid":
            body["usage"] = {
                "input_tokens": True,
                "output_tokens": -1,
                "cache_creation_input_tokens": "5",
                "cache_read_input_tokens": 0.5,
            }
        elif mode == "zero":
            body["usage"] = dict.fromkeys(usage, 0)
        elif mode == "cache_only":
            body["usage"] = {"cache_creation_input_tokens": 5, "cache_read_input_tokens": 7, "output_tokens": 2}
        else:
            body["usage"] = {**usage, "output_tokens": 2}
        return MessagesResult(status_code=200, headers={}, data=body)

    async def fake_stream(*args, **kwargs):
        nonlocal closed
        try:
            for block in blocks:
                if mode == "partial_error" and "message_stop" in block:
                    raise AnthropicUpstreamError(500, "api_error", "partial failure")
                if mode == "error_event" and "message_stop" in block:
                    yield 'event: error\ndata: {"type":"error","error":{"type":"api_error","message":"failed"}}\n\n'
                    return
                yield block
        finally:
            closed = True

    monkeypatch.setattr(anthropic_service_module, "core_request_messages", fake_request)
    monkeypatch.setattr(anthropic_service_module, "core_stream_messages", fake_stream)
    streaming = mode in {"stream", "partial_error", "error_event", "unconfirmed"}
    response = await async_client.post(
        "/v1/messages",
        json=_messages_payload(stream=streaming),
        headers={"Authorization": f"Bearer {key['key']}"},
    )
    assert response.status_code == 200
    if mode == "stream":
        assert response.text == "".join(blocks)
    if streaming:
        assert closed
    if mode == "unconfirmed":
        assert not health_states
        assert "event: error" in response.text
    if mode in {"failover", "partial_error", "error_event"}:
        assert health_states == (["released"] if mode == "failover" else ["finalized"])
    assert await app_instance.state.proxy_service.drain_persistence_tasks(timeout_seconds=5)
    async with SessionLocal() as session:
        log = (await session.execute(select(RequestLog).where(RequestLog.api_key_id == key["id"]))).scalar_one()
        assert (log.input_tokens, log.output_tokens, log.cached_input_tokens) == expected
        assert log.status == ("error" if mode in {"partial_error", "error_event", "unconfirmed"} else "success")
        assert log.cost_usd is None  # No Claude price is configured; never invent one.
        limit = (await session.execute(select(ApiKeyLimit).where(ApiKeyLimit.api_key_id == key["id"]))).scalar_one()
        assert limit.current_value == (expected[0] or 0) + (expected[1] or 0)
        reservations = list(
            (
                await session.execute(
                    select(ApiKeyUsageReservation).where(ApiKeyUsageReservation.api_key_id == key["id"])
                )
            ).scalars()
        )
        assert not any(row.status in {"reserved", "settling"} for row in reservations)
        assert sum(row.status == "finalized" for row in reservations) == int(expected[0] is not None)
        if mode == "failover":
            assert calls == 2
            assert len(reservations) == 2
        since = utcnow() - timedelta(hours=1)
        before = await RequestLogsRepository(session).aggregate_activity_since(since)
        assert (before.input_tokens, before.output_tokens, before.cached_input_tokens) == tuple(
            count or 0 for count in expected
        )
    assert await run_hourly_fold_pass(now=utcnow() + FOLD_LAG + timedelta(hours=2)) >= 1
    async with SessionLocal() as session:
        after = await RequestLogsRepository(session).aggregate_activity_since(since)
        assert after == before


@pytest.mark.asyncio
async def test_messages_disconnect_keeps_usage_and_closes_upstream(async_client, app_instance, monkeypatch):
    import json

    from app.db.models import ApiKeyLimit, ApiKeyUsageReservation, RequestLog

    await _seed_oauth_seat("disconnect_usage", "disconnect-usage@example.com")
    assert (await async_client.put("/api/settings", json={"apiKeyAuthEnabled": True})).status_code == 200
    created = await async_client.post(
        "/api/api-keys/",
        json={
            "name": "disconnect-usage",
            "limits": [{"limitType": "total_tokens", "limitWindow": "weekly", "maxValue": 1000}],
        },
    )
    assert created.status_code == 200
    key = created.json()
    delivered = asyncio.Event()
    closed = False
    health_writes = []

    async def fake_stream(*args, **kwargs):
        nonlocal closed
        try:
            yield (
                'event: message_start\ndata: {"type":"message_start","message":{"usage":'
                '{"input_tokens":3,"cache_read_input_tokens":7,"output_tokens":0}}}\n\n'
            )
            await asyncio.Event().wait()
        finally:
            closed = True

    async def health_spy(*args, **kwargs):
        health_writes.append(True)

    monkeypatch.setattr(anthropic_service_module, "core_stream_messages", fake_stream)
    monkeypatch.setattr(proxy_module.ProxyService, "_handle_proxy_error", health_spy)
    body = json.dumps(_messages_payload(stream=True)).encode()
    sent_request = False

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": body, "more_body": False}
        await delivered.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and b"message_start" in message.get("body", b""):
            delivered.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "root_path": "",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1),
        "headers": [(b"content-type", b"application/json"), (b"authorization", f"Bearer {key['key']}".encode())],
    }
    await asyncio.wait_for(app_instance(scope, receive, send), timeout=5)
    assert await app_instance.state.proxy_service.drain_persistence_tasks(timeout_seconds=5)
    assert delivered.is_set()
    assert closed
    assert not health_writes
    async with SessionLocal() as session:
        log = (await session.execute(select(RequestLog).where(RequestLog.api_key_id == key["id"]))).scalar_one()
        assert (log.status, log.error_code) == ("cancelled", "client_disconnected")
        assert (log.input_tokens, log.output_tokens, log.cached_input_tokens) == (10, 0, 7)
        limit = (await session.execute(select(ApiKeyLimit).where(ApiKeyLimit.api_key_id == key["id"]))).scalar_one()
        assert limit.current_value == 10
        reservation = (
            await session.execute(select(ApiKeyUsageReservation).where(ApiKeyUsageReservation.api_key_id == key["id"]))
        ).scalar_one()
        assert reservation.status == "finalized"
