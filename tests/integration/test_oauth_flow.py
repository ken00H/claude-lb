from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp import ClientSession, web
from fastapi.responses import JSONResponse

import app.modules.oauth.service as oauth_module
from app.core.auth import generate_unique_account_id
from app.core.clients.oauth import OAuthError, OAuthTokens
from app.core.crypto import TokenEncryptor
from app.core.utils.time import utcnow
from app.db.models import Account, AccountStatus, OAuthFlowState
from app.db.session import SessionLocal
from app.modules.accounts.repository import AccountsRepository
from app.modules.oauth import api as oauth_api_module
from app.modules.oauth.repository import OAuthFlowRepository
from app.modules.oauth.schemas import ManualCallbackRequest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _oauth_flow_schema(db_setup):
    """Ensure the shared schema (incl. ``oauth_flow_states``) exists.

    Every OAuth service path now persists flow state to the shared DB, so the
    service-level tests that construct ``OauthService`` directly need the table
    present, not only the ``async_client`` ones.
    """

    del db_setup


_injected_oauth_stores: list[oauth_module.OAuthStateStore] = []


async def _drain_global_oauth_store() -> None:
    """Reset the module-global OAuth store AND await its tasks to completion.

    ``_OAUTH_STORE.reset()`` cancels poll tasks but does not await them, so a
    cancelled (or still-pending) device poller can keep running into the next
    test on the shared session loop -- exchanging the device code against
    whatever ``exchange_device_token`` is monkeypatched to at that moment (or
    the real network client) and committing slot/status writes to the next
    test's freshly reset database (issue #1794, same family as #1755). Awaiting
    every task here guarantees nothing owned by the store crosses a test
    boundary, and retrieves cancelled tasks' exceptions so they cannot surface
    as unrelated "Task exception was never retrieved" noise in a later test.
    """

    store = oauth_module._OAUTH_STORE
    async with store.lock:
        tasks = [
            flow.poll_task for flow in store._flows.values() if flow.poll_task is not None and not flow.poll_task.done()
        ]
        stop_task = store._callback_server_stop_task
        if stop_task is not None and not stop_task.done():
            tasks.append(stop_task)
    await store.reset()
    for injected_store in _injected_oauth_stores:
        await injected_store.reset()
    _injected_oauth_stores.clear()
    for task in tasks:
        task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await task


@pytest.fixture(autouse=True)
async def _isolate_global_oauth_store():
    """Fence ``oauth_module._OAUTH_STORE`` at BOTH edges of every test.

    The per-test ``_OAUTH_STORE.reset()`` calls this fixture replaces only ran
    at each test's start, so whichever test happened to run next inherited the
    previous test's live poll tasks for its whole fixture setup -- the
    order-dependent "different victim per run" flake of issue #1794.
    """

    await _drain_global_oauth_store()
    yield
    await _drain_global_oauth_store()


def _oauth_state_token(authorization_url: str) -> str:
    parsed = urlparse(authorization_url)
    return parse_qs(parsed.query)["state"][0]


@pytest.mark.asyncio
async def test_manual_callback_api_sanitizes_unexpected_exception():
    class FailingOauthService:
        async def manual_callback(self, callback_url: str, flow_id: str | None = None):
            raise RuntimeError("Traceback (most recent call last): password=super-secret")

    response = cast(
        JSONResponse,
        await oauth_api_module.manual_callback(
            ManualCallbackRequest(callback_url="http://localhost:1455/?code=c&state=s"),
            context=cast(Any, SimpleNamespace(service=FailingOauthService())),
        ),
    )

    assert response.status_code == 500
    payload = json.loads(bytes(response.body))
    assert payload == {
        "error": {
            "code": "manual_callback_failed",
            "message": "An internal error occurred.",
        }
    }
    assert "super-secret" not in bytes(response.body).decode()


@pytest.mark.asyncio
async def test_manual_callback_api_preserves_oauth_error():
    class FailingOauthService:
        async def manual_callback(self, callback_url: str, flow_id: str | None = None):
            raise OAuthError("invalid_grant", "Authorization code expired", status_code=400)

    response = cast(
        JSONResponse,
        await oauth_api_module.manual_callback(
            ManualCallbackRequest(callback_url="http://localhost:1455/?code=c&state=s"),
            context=cast(Any, SimpleNamespace(service=FailingOauthService())),
        ),
    )

    assert response.status_code == 502
    assert json.loads(bytes(response.body)) == {
        "error": {
            "code": "invalid_grant",
            "message": "Authorization code expired",
        }
    }


@pytest.mark.asyncio
async def test_manual_callback_service_sanitizes_unexpected_exception(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=oauth_module.logger.name)
    # Persist the flow durably (real flows are written to the shared DB at start)
    # so the reconciliation gate keeps it rather than dropping it as stale.
    async with SessionLocal() as session:
        await OAuthFlowRepository(session, TokenEncryptor()).create(
            oauth_module.OAuthFlowRecord(
                flow_id="flow-1",
                method="browser",
                status="pending",
                state_token="state-1",
                code_verifier="verifier-1",
            )
        )
    async with oauth_module._OAUTH_STORE.lock:
        oauth_module._OAUTH_STORE.remember_flow_locked(
            oauth_module.OAuthState(
                flow_id="flow-1",
                status="pending",
                method="browser",
                state_token="state-1",
                code_verifier="verifier-1",
            )
        )

    async def fake_oauth_route():
        return None

    async def fake_exchange_authorization_code(**_kwargs):
        raise RuntimeError("Unexpected error: /home/app/password.txt")

    monkeypatch.setattr(oauth_module, "_oauth_route", fake_oauth_route)
    monkeypatch.setattr(oauth_module, "exchange_authorization_code", fake_exchange_authorization_code)
    service = oauth_module.OauthService(cast(AccountsRepository, SimpleNamespace()))

    response = await service.manual_callback("http://localhost:1455/?code=code-1&state=state-1", flow_id="flow-1")

    assert response.status == "error"
    assert response.error_message == "An internal error occurred."
    assert "RuntimeError" in caplog.text
    assert "password.txt" not in caplog.text
    assert "/home/app" not in caplog.text
    assert "Traceback" not in caplog.text
    async with oauth_module._OAUTH_STORE.lock:
        flow = oauth_module._OAUTH_STORE.get_flow_locked("flow-1")
        assert flow is not None
        assert flow.error_message == "An internal error occurred."


def test_oauth_error_html_escapes_message():
    html = oauth_module._error_html("bad <script>alert('x')</script>")

    assert "<script>" not in html
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in html


@pytest.mark.asyncio
async def test_browser_oauth_flow_creates_oauth_seat_account(async_client, monkeypatch):
    """Full browser + manual-callback round trip against mocked Anthropic
    endpoints, using the copy/paste ``CODE#STATE`` callback form (Anthropic's
    primary flow; the device flow no longer exists)."""

    async def fake_callback_server_start(self) -> None:
        return None

    async def fake_oauth_route():
        return None

    async def fake_exchange_authorization_code(**_):
        return OAuthTokens(
            access_token="seat-access-token",
            refresh_token="seat-refresh-token",
            id_token=None,
            expires_in=3600,
            scope="user:inference user:profile",
            organization_id="org-seat",
            account_id="acc_seat",
            email="seat@example.com",
        )

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)
    monkeypatch.setattr(oauth_module, "_oauth_route", fake_oauth_route)
    monkeypatch.setattr(oauth_module, "exchange_authorization_code", fake_exchange_authorization_code)

    start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert start.status_code == 200
    payload = start.json()
    assert payload["method"] == "browser"

    async with oauth_module._OAUTH_STORE.lock:
        state_token = oauth_module._OAUTH_STORE.state.state_token

    response = await async_client.post(
        "/api/oauth/manual-callback",
        json={"callbackUrl": f"code-seat#{state_token}"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "success", "errorMessage": None}

    status = await async_client.get("/api/oauth/status")
    assert status.status_code == 200
    assert status.json()["status"] == "success"

    expected_account_id = generate_unique_account_id("acc_seat", "seat@example.com")
    async with SessionLocal() as session:
        stored = await AccountsRepository(session).get_by_id(expected_account_id)
    assert stored is not None
    assert stored.pool_class == "oauth_seat"
    assert stored.anthropic_account_id == "acc_seat"
    assert stored.anthropic_organization_id == "org-seat"
    assert stored.token_expires_at is not None and stored.token_expires_at > int(time.time())
    assert stored.id_token_encrypted is None


@pytest.mark.asyncio
async def test_browser_oauth_reauth_reuses_existing_row_for_same_anthropic_identity(
    async_client,
    monkeypatch,
):
    """OAuth reauth for the same Anthropic identity must reuse the existing
    local row even when ``importWithoutOverwrite`` is enabled.

    Before #788, this code path created an ``__copyN`` row whenever the
    operator had toggled ``importWithoutOverwrite`` on, because the
    dashboard's side-by-side import setting was incorrectly conflated
    with reauth. The reauth path always reconciles to one local row per
    upstream identity, so a refresh-token-revoked account picks up the
    new tokens onto its historical row instead of forking a duplicate.
    """

    settings = await async_client.put(
        "/api/settings",
        json={
            "stickyThreadsEnabled": False,
            "preferEarlierResetAccounts": False,
            "importWithoutOverwrite": True,
            "totpRequiredOnLogin": False,
        },
    )
    assert settings.status_code == 200
    assert settings.json()["importWithoutOverwrite"] is True

    email = "reauth@example.com"
    raw_account_id = "acc_reauth"
    call_count = {"value": 0}

    async def fake_callback_server_start(self) -> None:
        return None

    async def fake_exchange_authorization_code(**_):
        call_count["value"] += 1
        return OAuthTokens(
            access_token=f"access-token-{call_count['value']}",
            refresh_token=f"refresh-token-{call_count['value']}",
            id_token=None,
            expires_in=3600,
            organization_id="org-reauth",
            account_id=raw_account_id,
            email=email,
        )

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)
    monkeypatch.setattr(oauth_module, "exchange_authorization_code", fake_exchange_authorization_code)

    async def _run_flow_once() -> None:
        start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
        assert start.status_code == 200
        assert start.json()["method"] == "browser"
        flow_id = start.json()["flowId"]
        async with oauth_module._OAUTH_STORE.lock:
            state_token = oauth_module._OAUTH_STORE.state.state_token
        response = await async_client.post(
            "/api/oauth/manual-callback",
            json={"callbackUrl": f"code-reauth-{flow_id}#{state_token}"},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "success", "errorMessage": None}

    await _run_flow_once()
    await _run_flow_once()

    expected_account_id = generate_unique_account_id(raw_account_id, email)
    async with SessionLocal() as session:
        stored = await AccountsRepository(session).get_by_id(expected_account_id)
        assert stored is not None
        # The second flow's rotated tokens landed on the historical row...
        assert TokenEncryptor().decrypt(stored.access_token_encrypted) == "access-token-2"
        assert TokenEncryptor().decrypt(stored.refresh_token_encrypted) == "refresh-token-2"
        # ...and its Anthropic identity survived the replacement merge.
        assert stored.pool_class == "oauth_seat"
        assert stored.anthropic_account_id == raw_account_id
        assert stored.status == AccountStatus.ACTIVE
        assert stored.deactivation_reason is None

    accounts = await async_client.get("/api/accounts")
    assert accounts.status_code == 200
    data = [account for account in accounts.json()["accounts"] if account["email"] == email]
    assert len(data) == 1
    assert data[0]["accountId"] == expected_account_id


@pytest.mark.asyncio
async def test_oauth_persist_tokens_invalidates_routing_caches_after_identity_merge(monkeypatch):
    repo = AsyncMock()
    service = oauth_module.OauthService(repo)
    account_cache = SimpleNamespace(invalidated=False)

    def _invalidate_account_cache() -> None:
        account_cache.invalidated = True

    account_cache.invalidate = _invalidate_account_cache
    api_key_cache = SimpleNamespace(cleared=False)

    def _clear_api_key_cache() -> None:
        api_key_cache.cleared = True

    api_key_cache.clear = _clear_api_key_cache
    poller = SimpleNamespace(bumped=[])

    async def _bump(namespace: str) -> None:
        poller.bumped.append(namespace)

    poller.bump = _bump
    monkeypatch.setattr(oauth_module, "get_account_selection_cache", lambda: account_cache, raising=False)
    monkeypatch.setattr(oauth_module, "get_api_key_cache", lambda: api_key_cache, raising=False)
    monkeypatch.setattr(oauth_module, "get_cache_invalidation_poller", lambda: poller, raising=False)
    monkeypatch.setattr(oauth_module, "NAMESPACE_API_KEY", "api_key", raising=False)

    await service._persist_tokens(
        OAuthTokens(
            access_token="access-token",
            refresh_token="refresh-token",
            id_token=None,
            account_id="acc_reauth_cache",
            email="reauth-cache@example.com",
        )
    )

    repo.upsert.assert_not_awaited()
    repo.upsert_account_slot.assert_awaited_once()
    assert repo.upsert_account_slot.await_args.kwargs == {
        "preserve_unknown_workspace_duplicates": False,
        "preserve_identity_slots": True,
    }
    assert account_cache.invalidated is True
    assert api_key_cache.cleared is True
    assert poller.bumped == ["api_key"]


@pytest.mark.asyncio
async def test_targeted_reauth_replaces_matching_anthropic_seat(monkeypatch):
    repo = AsyncMock()
    service = oauth_module.OauthService(repo)
    monkeypatch.setattr(oauth_module, "get_account_selection_cache", lambda: SimpleNamespace(invalidate=lambda: None))
    monkeypatch.setattr(oauth_module, "get_api_key_cache", lambda: SimpleNamespace(clear=lambda: None))
    monkeypatch.setattr(oauth_module, "get_cache_invalidation_poller", lambda: None)

    target_id = "anth-seat-a"
    intended = Account(
        id=target_id,
        pool_class="oauth_seat",
        anthropic_account_id="anth-seat-a",
        email="seat-a@example.com",
        plan_type="max",
        access_token_encrypted=service._encryptor.encrypt("old-access"),
        refresh_token_encrypted=service._encryptor.encrypt("old-refresh"),
        last_refresh=utcnow(),
        status=AccountStatus.REAUTH_REQUIRED,
    )
    repo.get_by_id.return_value = intended
    repo.replace_reauthorized.side_effect = lambda _account_id, account: account

    await service._persist_tokens(
        OAuthTokens(
            access_token="new-access",
            refresh_token="new-refresh",
            id_token=None,
            expires_in=3600,
            organization_id="org-seat-a",
            account_id="anth-seat-a",
            email="seat-a@example.com",
        ),
        intended_account_id=target_id,
    )

    repo.replace_reauthorized.assert_awaited_once()
    assert repo.replace_reauthorized.await_args.args[0] == target_id
    saved = repo.replace_reauthorized.await_args.args[1]
    assert saved.anthropic_account_id == "anth-seat-a"
    assert saved.pool_class == "oauth_seat"
    repo.upsert_account_slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_targeted_reauth_rejects_different_known_anthropic_seat(monkeypatch):
    repo = AsyncMock()
    service = oauth_module.OauthService(repo)
    target_id = "anth-seat-a"
    intended = Account(
        id=target_id,
        pool_class="oauth_seat",
        anthropic_account_id="anth-seat-a",
        email="seat-a@example.com",
        plan_type="max",
        access_token_encrypted=service._encryptor.encrypt("old-access"),
        refresh_token_encrypted=service._encryptor.encrypt("old-refresh"),
        last_refresh=utcnow(),
        status=AccountStatus.REAUTH_REQUIRED,
    )
    repo.get_by_id.return_value = intended

    with pytest.raises(oauth_module.ReauthSeatMismatchError):
        await service._persist_tokens(
            OAuthTokens(
                access_token="other-access",
                refresh_token="other-refresh",
                id_token=None,
                expires_in=3600,
                organization_id="org-seat-b",
                account_id="anth-seat-b",
                email="seat-b@example.com",
            ),
            intended_account_id=target_id,
        )

    repo.replace_reauthorized.assert_not_awaited()
    repo.upsert_account_slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_targeted_reauth_allows_unknown_seat_uuid_on_either_side(monkeypatch):
    """A missing uuid on the intended row (identity never populated) or on the
    callback stays permissive so an operator can still repair the row; only a
    *differing known* uuid is a mismatch."""

    repo = AsyncMock()
    service = oauth_module.OauthService(repo)
    monkeypatch.setattr(oauth_module, "get_account_selection_cache", lambda: SimpleNamespace(invalidate=lambda: None))
    monkeypatch.setattr(oauth_module, "get_api_key_cache", lambda: SimpleNamespace(clear=lambda: None))
    monkeypatch.setattr(oauth_module, "get_cache_invalidation_poller", lambda: None)

    # Intended row with no stored uuid.
    intended_without_uuid = Account(
        id="anth-unknown-seat",
        pool_class="oauth_seat",
        email="unknown-seat@example.com",
        plan_type="unknown",
        access_token_encrypted=service._encryptor.encrypt("old-access"),
        refresh_token_encrypted=service._encryptor.encrypt("old-refresh"),
        last_refresh=utcnow(),
        status=AccountStatus.REAUTH_REQUIRED,
    )
    repo.get_by_id.return_value = intended_without_uuid
    repo.replace_reauthorized.side_effect = lambda _account_id, account: account

    await service._persist_tokens(
        OAuthTokens(
            access_token="new-access",
            refresh_token="new-refresh",
            id_token=None,
            account_id="anth-fresh-uuid",
            email="unknown-seat@example.com",
        ),
        intended_account_id="anth-unknown-seat",
    )
    repo.replace_reauthorized.assert_awaited_once()

    # Callback carrying no uuid against a known intended seat.
    repo.replace_reauthorized.reset_mock()
    intended_with_uuid = Account(
        id="anth-known-seat",
        pool_class="oauth_seat",
        anthropic_account_id="anth-known-seat-uuid",
        email="known-seat@example.com",
        plan_type="max",
        access_token_encrypted=service._encryptor.encrypt("old-access"),
        refresh_token_encrypted=service._encryptor.encrypt("old-refresh"),
        last_refresh=utcnow(),
        status=AccountStatus.REAUTH_REQUIRED,
    )
    repo.get_by_id.return_value = intended_with_uuid

    await service._persist_tokens(
        OAuthTokens(
            access_token="new-access",
            refresh_token="new-refresh",
            id_token=None,
            account_id=None,
            email="known-seat@example.com",
        ),
        intended_account_id="anth-known-seat",
    )
    repo.replace_reauthorized.assert_awaited_once()
    repo.upsert_account_slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_targeted_reauth_rejects_missing_intended_account():
    repo = AsyncMock()
    service = oauth_module.OauthService(repo)
    repo.get_by_id.return_value = None

    with pytest.raises(oauth_module.ReauthSeatMismatchError):
        await service._persist_tokens(
            OAuthTokens(
                access_token="new-access",
                refresh_token="new-refresh",
                id_token=None,
                account_id="anth-vanished",
                email="vanishing@example.com",
            ),
            intended_account_id="anth-vanished-seat",
        )

    repo.replace_reauthorized.assert_not_awaited()
    repo.upsert_account_slot.assert_not_awaited()


@pytest.mark.asyncio
async def test_oauth_start_with_existing_account_marks_success(async_client):
    encryptor = TokenEncryptor()
    account = Account(
        id="acc_existing",
        email="existing@example.com",
        plan_type="plus",
        access_token_encrypted=encryptor.encrypt("access"),
        refresh_token_encrypted=encryptor.encrypt("refresh"),
        id_token_encrypted=encryptor.encrypt("id"),
        last_refresh=utcnow(),
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )
    async with SessionLocal() as session:
        repo = AccountsRepository(session)
        await repo.upsert(account)

    start = await async_client.post("/api/oauth/start", json={})
    assert start.status_code == 200
    assert start.json()["method"] == "browser"

    status = await async_client.get("/api/oauth/status")
    assert status.status_code == 200
    assert status.json()["status"] == "success"


@pytest.mark.asyncio
async def test_oauth_start_with_existing_account_clears_stale_flows(async_client, monkeypatch):
    async def fake_callback_server_start(self) -> None:
        return None

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)

    stale_start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert stale_start.status_code == 200
    stale_payload = stale_start.json()
    assert stale_payload["flowId"]

    async with oauth_module._OAUTH_STORE.lock:
        assert oauth_module._OAUTH_STORE._flows
        assert oauth_module._OAUTH_STORE._state_token_index

    encryptor = TokenEncryptor()
    account = Account(
        id="acc_existing_after_stale_flow",
        email="existing-after-stale-flow@example.com",
        plan_type="plus",
        access_token_encrypted=encryptor.encrypt("access"),
        refresh_token_encrypted=encryptor.encrypt("refresh"),
        id_token_encrypted=encryptor.encrypt("id"),
        last_refresh=utcnow(),
        status=AccountStatus.ACTIVE,
        deactivation_reason=None,
    )
    async with SessionLocal() as session:
        repo = AccountsRepository(session)
        await repo.upsert(account)

    start = await async_client.post("/api/oauth/start", json={})
    assert start.status_code == 200
    assert start.json()["method"] == "browser"

    status = await async_client.get("/api/oauth/status")
    assert status.status_code == 200
    assert status.json() == {"status": "success", "errorMessage": None}

    async with oauth_module._OAUTH_STORE.lock:
        assert oauth_module._OAUTH_STORE._flows == {}
        assert oauth_module._OAUTH_STORE._state_token_index == {}


@pytest.mark.asyncio
async def test_terminal_oauth_flows_are_bounded_outside_full_reset():
    retained_limit = oauth_module._MAX_RETAINED_TERMINAL_OAUTH_FLOWS

    async with oauth_module._OAUTH_STORE.lock:
        for index in range(retained_limit + 2):
            flow = oauth_module.OAuthState(
                flow_id=f"flow-{index}",
                status="pending",
                method="browser",
                state_token=f"state-{index}",
                code_verifier=f"verifier-{index}",
            )
            oauth_module._OAUTH_STORE.remember_flow_locked(flow)
            oauth_module._OAUTH_STORE.set_flow_status_locked(
                flow,
                status="error",
                error_message=f"failure-{index}",
            )

        assert len(oauth_module._OAUTH_STORE._flows) == retained_limit
        assert "flow-0" not in oauth_module._OAUTH_STORE._flows
        assert "flow-1" not in oauth_module._OAUTH_STORE._flows
        assert "state-0" not in oauth_module._OAUTH_STORE._state_token_index
        assert "state-1" not in oauth_module._OAUTH_STORE._state_token_index
        assert f"flow-{retained_limit + 1}" in oauth_module._OAUTH_STORE._flows
        assert oauth_module._OAUTH_STORE.state.error_message == f"failure-{retained_limit + 1}"


@pytest.mark.asyncio
async def test_expired_pending_browser_oauth_flows_are_pruned():
    now = time.time()
    async with oauth_module._OAUTH_STORE.lock:
        expired = oauth_module.OAuthState(
            flow_id="expired-flow",
            status="pending",
            method="browser",
            state_token="expired-state",
            code_verifier="expired-verifier",
            expires_at=now - 1,
        )
        active = oauth_module.OAuthState(
            flow_id="active-flow",
            status="pending",
            method="browser",
            state_token="active-state",
            code_verifier="active-verifier",
            expires_at=now + oauth_module._PENDING_BROWSER_OAUTH_FLOW_TTL_SECONDS,
        )
        oauth_module._OAUTH_STORE.remember_flow_locked(expired)
        oauth_module._OAUTH_STORE.remember_flow_locked(active)

        assert oauth_module._OAUTH_STORE.has_pending_browser_flows_locked()
        assert "expired-flow" not in oauth_module._OAUTH_STORE._flows
        assert "expired-state" not in oauth_module._OAUTH_STORE._state_token_index
        assert oauth_module._OAUTH_STORE.state.flow_id == "active-flow"


@pytest.mark.asyncio
async def test_only_expired_pending_browser_flow_no_longer_keeps_callback_server_alive():
    async with oauth_module._OAUTH_STORE.lock:
        flow = oauth_module.OAuthState(
            flow_id="expired-flow",
            status="pending",
            method="browser",
            state_token="expired-state",
            code_verifier="expired-verifier",
            expires_at=time.time() - 1,
        )
        oauth_module._OAUTH_STORE.remember_flow_locked(flow)

        assert not oauth_module._OAUTH_STORE.has_pending_browser_flows_locked()
        assert oauth_module._OAUTH_STORE._flows == {}
        assert oauth_module._OAUTH_STORE.state.status == "idle"


async def _wait_for_browser_flow_removal(store: oauth_module.OAuthStateStore, flow_id: str) -> None:
    async with asyncio.timeout(5):
        while flow_id in store._flows:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_abandoned_browser_flow_expiry_releases_callback_port_without_followup_request(
    monkeypatch, unused_tcp_port, async_client
):
    monkeypatch.setattr(oauth_module, "OAUTH_CALLBACK_PORT", unused_tcp_port)
    monkeypatch.setattr(oauth_module, "_PENDING_BROWSER_OAUTH_FLOW_TTL_SECONDS", 0.25)
    # Keep the flow pending until the listener is observed, even on a slow runner.
    now = time.time()
    monkeypatch.setattr(oauth_module, "time", SimpleNamespace(time=lambda: now))
    stopped = asyncio.Event()
    original_stop = oauth_module.OAuthCallbackServer.stop

    async def observe_stop(server):
        await original_stop(server)
        stopped.set()

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "stop", observe_stop)
    response = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert response.status_code == 200
    flow_id = response.json()["flowId"]
    _, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
    writer.close()
    await writer.wait_closed()

    now += oauth_module._PENDING_BROWSER_OAUTH_FLOW_TTL_SECONDS
    # Only the deadline can trigger cleanup: no status, callback, or new start.
    await asyncio.wait_for(stopped.wait(), timeout=5)
    store = oauth_module._OAUTH_STORE
    assert flow_id not in store._flows
    assert store._state_token_index == {}
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", unused_tcp_port)

    # A fresh listener can immediately acquire the released port.
    async def handler(_request):
        return web.Response(text="ok")

    replacement = oauth_module.OAuthCallbackServer(handler, port=unused_tcp_port)
    try:
        await replacement.start()
    finally:
        await replacement.stop()


@pytest.mark.asyncio
async def test_overlapping_browser_flows_keep_listener_until_final_expiry(monkeypatch, unused_tcp_port, async_client):
    monkeypatch.setattr(oauth_module, "OAUTH_CALLBACK_PORT", unused_tcp_port)
    monkeypatch.setattr(oauth_module, "_PENDING_BROWSER_OAUTH_FLOW_TTL_SECONDS", 0.25)
    first = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert first.status_code == 200
    monkeypatch.setattr(oauth_module, "_PENDING_BROWSER_OAUTH_FLOW_TTL_SECONDS", 1)
    second = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert second.status_code == 200
    store = oauth_module._OAUTH_STORE
    await _wait_for_browser_flow_removal(store, first.json()["flowId"])
    assert second.json()["flowId"] in store._flows
    _, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
    writer.close()
    await writer.wait_closed()

    await _wait_for_browser_flow_removal(store, second.json()["flowId"])
    async with asyncio.timeout(5):
        while store._callback_server is not None:
            await asyncio.sleep(0.01)
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", unused_tcp_port)


@pytest.mark.asyncio
async def test_hydrated_browser_flow_rearms_earlier_deadline_and_reset_drains_task(monkeypatch):
    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", AsyncMock())
    source = _make_replica_service(oauth_module.OAuthStateStore())
    replica = _make_replica_service(oauth_module.OAuthStateStore())
    later = await replica._start_browser_flow()
    monkeypatch.setattr(oauth_module, "_PENDING_BROWSER_OAUTH_FLOW_TTL_SECONDS", 0.25)
    earlier = await source._start_browser_flow()
    assert earlier.flow_id is not None
    assert (await replica.oauth_status(earlier.flow_id)).status == "pending"

    await _wait_for_browser_flow_removal(replica._store, earlier.flow_id)
    assert later.flow_id in replica._store._flows
    task = replica._store._browser_flow_expiry_task
    assert task is not None and not task.done()
    await replica._store.reset()
    assert task.done()
    assert replica._store._browser_flow_expiry_task is None


@pytest.mark.asyncio
async def test_callback_access_log_omits_code_and_state(caplog, unused_tcp_port):
    code_secret = "CALLBACK_CODE_SECRET"
    state_secret = "CALLBACK_STATE_SECRET"

    async def handler(request: web.Request) -> web.StreamResponse:
        assert request.query["code"] == code_secret
        assert request.query["state"] == state_secret
        return web.Response(text="callback accepted")

    caplog.set_level(logging.INFO, logger="aiohttp.access")
    server = oauth_module.OAuthCallbackServer(handler, port=unused_tcp_port)
    await server.start()
    try:
        async with ClientSession() as client:
            async with client.get(
                f"http://127.0.0.1:{unused_tcp_port}/auth/callback",
                params={"code": code_secret, "state": state_secret},
            ) as response:
                status = response.status
                body = await response.text()
    finally:
        await server.stop()

    assert status == 200
    assert body == "callback accepted"
    assert code_secret not in caplog.text
    assert state_secret not in caplog.text


@pytest.mark.asyncio
async def test_callback_server_remains_reserved_until_stop_completes():
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()

    class FakeCallbackServer:
        async def stop(self) -> None:
            stop_started.set()
            await release_stop.wait()

    fake_server = FakeCallbackServer()
    async with SessionLocal() as session:
        service = oauth_module.OauthService(AccountsRepository(session))
        async with oauth_module._OAUTH_STORE.lock:
            oauth_module._OAUTH_STORE._callback_server = cast(oauth_module.OAuthCallbackServer, fake_server)

        stop_task = asyncio.create_task(service._stop_callback_server_if_idle())
        await stop_started.wait()
        assert oauth_module._OAUTH_STORE._callback_server is fake_server

        release_stop.set()
        await stop_task
        assert oauth_module._OAUTH_STORE._callback_server is None


@pytest.mark.asyncio
async def test_oauth_start_reports_callback_server_unavailable_on_os_error(async_client, monkeypatch):
    """Anthropic has no device flow to fall back to: a busy callback port is a
    hard error for the browser flow (the manual-callback path still works)."""

    async def fake_browser_flow(self, *, intended_account_id=None):
        raise OSError("no port")

    monkeypatch.setattr(oauth_module.OauthService, "_start_browser_flow", fake_browser_flow)

    start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert start.status_code == 502
    assert start.json()["error"]["code"] == "callback_server_unavailable"


@pytest.mark.asyncio
async def test_oauth_start_rejects_removed_device_flow(async_client):
    start = await async_client.post("/api/oauth/start", json={"forceMethod": "device"})

    assert start.status_code == 502
    assert start.json()["error"]["code"] == "device_flow_unsupported"


@pytest.mark.asyncio
async def test_manual_callback_returns_success_and_creates_account(async_client, monkeypatch):
    async def fake_callback_server_start(self) -> None:
        return None

    email = "manual@example.com"
    raw_account_id = "acc_manual"

    async def fake_exchange_authorization_code(**_):
        return OAuthTokens(
            access_token="manual-access-token",
            refresh_token="manual-refresh-token",
            id_token=None,
            expires_in=3600,
            organization_id="org_manual",
            account_id=raw_account_id,
            email=email,
        )

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)
    monkeypatch.setattr(oauth_module, "exchange_authorization_code", fake_exchange_authorization_code)

    start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert start.status_code == 200
    payload = start.json()
    assert payload["method"] == "browser"

    async with oauth_module._OAUTH_STORE.lock:
        state_token = oauth_module._OAUTH_STORE.state.state_token

    response = await async_client.post(
        "/api/oauth/manual-callback",
        json={
            "callbackUrl": f"http://localhost:1455/auth/callback?code=manual-code&state={state_token}",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "success", "errorMessage": None}

    status = await async_client.get("/api/oauth/status")
    assert status.status_code == 200
    assert status.json()["status"] == "success"

    expected_account_id = generate_unique_account_id(raw_account_id, email)
    accounts = await async_client.get("/api/accounts")
    assert accounts.status_code == 200
    data = accounts.json()["accounts"]
    assert any(account["accountId"] == expected_account_id for account in data)


@pytest.mark.asyncio
async def test_manual_callback_returns_error_message_for_invalid_state(async_client, monkeypatch):
    async def fake_callback_server_start(self) -> None:
        return None

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)

    start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert start.status_code == 200
    payload = start.json()
    assert payload["method"] == "browser"

    response = await async_client.post(
        "/api/oauth/manual-callback",
        json={
            "callbackUrl": "http://localhost:1455/auth/callback?code=manual-code&state=wrong",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "error",
        "errorMessage": "Invalid OAuth callback: state mismatch or missing code.",
    }

    status = await async_client.get("/api/oauth/status")
    assert status.status_code == 200
    assert status.json() == {"status": "pending", "errorMessage": None}

    flow_status = await async_client.get("/api/oauth/status", params={"flowId": payload["flowId"]})
    assert flow_status.status_code == 200
    assert flow_status.json() == {"status": "pending", "errorMessage": None}


@pytest.mark.asyncio
async def test_oauth_status_binds_camel_case_flow_id(async_client, monkeypatch):
    async def fake_callback_server_start(self) -> None:
        return None

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)

    first_start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert first_start.status_code == 200
    first_payload = first_start.json()
    assert first_payload["flowId"]

    second_start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert second_start.status_code == 200
    second_payload = second_start.json()
    assert second_payload["flowId"]
    assert second_payload["flowId"] != first_payload["flowId"]

    error_response = await async_client.post(
        "/api/oauth/manual-callback",
        json={
            "callbackUrl": "http://localhost:1455/auth/callback?code=manual-code&state=wrong",
            "flowId": second_payload["flowId"],
        },
    )
    assert error_response.status_code == 200
    assert error_response.json()["status"] == "error"

    first_status = await async_client.get("/api/oauth/status", params={"flowId": first_payload["flowId"]})
    assert first_status.status_code == 200
    assert first_status.json() == {"status": "pending", "errorMessage": None}

    second_status = await async_client.get("/api/oauth/status", params={"flowId": second_payload["flowId"]})
    assert second_status.status_code == 200
    assert second_status.json() == {"status": "pending", "errorMessage": None}

    typo_status = await async_client.get("/api/oauth/status", params={"flowId": f"{second_payload['flowId']}-typo"})
    assert typo_status.status_code == 200
    assert typo_status.json() == {"status": "pending", "errorMessage": None}

    latest_status = await async_client.get("/api/oauth/status")
    assert latest_status.status_code == 200
    assert latest_status.json() == {"status": "pending", "errorMessage": None}


@pytest.mark.asyncio
async def test_manual_callback_error_resolves_state_before_marking_flow_failed(async_client, monkeypatch):
    async def fake_callback_server_start(self) -> None:
        return None

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)

    first_start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert first_start.status_code == 200
    first_payload = first_start.json()
    first_error_url = (
        "http://localhost:1455/auth/callback?error=access_denied&state="
        f"{_oauth_state_token(first_payload['authorizationUrl'])}"
    )

    second_start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert second_start.status_code == 200
    second_payload = second_start.json()
    assert second_payload["flowId"] != first_payload["flowId"]

    mismatched_response = await async_client.post(
        "/api/oauth/manual-callback",
        json={
            "callbackUrl": first_error_url,
            "flowId": second_payload["flowId"],
        },
    )
    assert mismatched_response.status_code == 200
    assert mismatched_response.json() == {
        "status": "error",
        "errorMessage": "OAuth error: access_denied",
    }

    first_status = await async_client.get("/api/oauth/status", params={"flowId": first_payload["flowId"]})
    assert first_status.status_code == 200
    assert first_status.json() == {"status": "pending", "errorMessage": None}

    second_status = await async_client.get("/api/oauth/status", params={"flowId": second_payload["flowId"]})
    assert second_status.status_code == 200
    assert second_status.json() == {"status": "pending", "errorMessage": None}

    matching_response = await async_client.post(
        "/api/oauth/manual-callback",
        json={
            "callbackUrl": first_error_url,
            "flowId": first_payload["flowId"],
        },
    )
    assert matching_response.status_code == 200
    assert matching_response.json() == {
        "status": "error",
        "errorMessage": "OAuth error: access_denied",
    }

    first_status = await async_client.get("/api/oauth/status", params={"flowId": first_payload["flowId"]})
    assert first_status.status_code == 200
    assert first_status.json() == {
        "status": "error",
        "errorMessage": "OAuth error: access_denied",
    }

    second_status = await async_client.get("/api/oauth/status", params={"flowId": second_payload["flowId"]})
    assert second_status.status_code == 200
    assert second_status.json() == {"status": "pending", "errorMessage": None}


@pytest.mark.asyncio
async def test_unknown_flow_error_does_not_mutate_latest_oauth_status():
    async with SessionLocal() as session:
        service = oauth_module.OauthService(AccountsRepository(session))

        async with oauth_module._OAUTH_STORE.lock:
            latest = oauth_module.OAuthState(
                flow_id="latest-flow",
                status="pending",
                method="browser",
                state_token="latest-state",
                code_verifier="latest-verifier",
            )
            oauth_module._OAUTH_STORE.remember_flow_locked(latest)
            oauth_module._OAUTH_STORE.set_latest_flow_locked(latest)

        await service._set_error("wrong flow", flow_id="missing-flow")

        async with oauth_module._OAUTH_STORE.lock:
            latest_state = oauth_module._OAUTH_STORE.state
            latest_flow = oauth_module._OAUTH_STORE.get_flow_locked("latest-flow")

    assert latest_state.status == "pending"
    assert latest_state.error_message is None
    assert latest_flow is not None
    assert latest_flow.status == "pending"
    assert latest_flow.error_message is None


@pytest.mark.asyncio
async def test_missing_flow_error_does_not_mutate_latest_oauth_status():
    async with SessionLocal() as session:
        service = oauth_module.OauthService(AccountsRepository(session))

        async with oauth_module._OAUTH_STORE.lock:
            latest = oauth_module.OAuthState(
                flow_id="latest-flow",
                status="pending",
                method="browser",
                state_token="latest-state",
                code_verifier="latest-verifier",
            )
            oauth_module._OAUTH_STORE.remember_flow_locked(latest)
            oauth_module._OAUTH_STORE.set_latest_flow_locked(latest)

        await service._set_error("wrong flow")

        async with oauth_module._OAUTH_STORE.lock:
            latest_state = oauth_module._OAUTH_STORE.state
            latest_flow = oauth_module._OAUTH_STORE.get_flow_locked("latest-flow")

    assert latest_state.status == "pending"
    assert latest_state.error_message is None
    assert latest_flow is not None
    assert latest_flow.status == "pending"
    assert latest_flow.error_message is None


@pytest.mark.asyncio
async def test_manual_callback_unknown_state_does_not_mutate_latest_flow(async_client, monkeypatch):
    async def fake_callback_server_start(self) -> None:
        return None

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)

    start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert start.status_code == 200
    payload = start.json()

    response = await async_client.post(
        "/api/oauth/manual-callback",
        json={
            "callbackUrl": "http://localhost:1455/auth/callback?error=access_denied&state=missing-state",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "error",
        "errorMessage": "OAuth error: access_denied",
    }

    status = await async_client.get("/api/oauth/status", params={"flowId": payload["flowId"]})
    assert status.status_code == 200
    assert status.json() == {"status": "pending", "errorMessage": None}


@pytest.mark.asyncio
async def test_concurrent_browser_oauth_flows_keep_callbacks_isolated(async_client, monkeypatch):
    async def fake_callback_server_start(self) -> None:
        return None

    async def fake_exchange_authorization_code(**kwargs):
        code = kwargs["code"]
        return OAuthTokens(
            access_token=f"access-{code}",
            refresh_token=f"refresh-{code}",
            id_token=None,
            expires_in=3600,
            organization_id=f"org_{code}",
            account_id=f"acc_{code}",
            email=f"{code}@example.com",
        )

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)
    monkeypatch.setattr(oauth_module, "exchange_authorization_code", fake_exchange_authorization_code)

    first_start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert first_start.status_code == 200
    first_payload = first_start.json()
    assert first_payload["method"] == "browser"
    assert first_payload["flowId"]

    second_start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert second_start.status_code == 200
    second_payload = second_start.json()
    assert second_payload["method"] == "browser"
    assert second_payload["flowId"]
    assert second_payload["flowId"] != first_payload["flowId"]

    first_response = await async_client.post(
        "/api/oauth/manual-callback",
        json={
            "callbackUrl": (
                f"http://localhost:1455/auth/callback?code=code-first&state="
                f"{_oauth_state_token(first_payload['authorizationUrl'])}"
            ),
            "flowId": first_payload["flowId"],
        },
    )
    assert first_response.status_code == 200
    assert first_response.json() == {"status": "success", "errorMessage": None}

    second_response = await async_client.post(
        "/api/oauth/manual-callback",
        json={
            "callbackUrl": (
                f"http://localhost:1455/auth/callback?code=code-second&state="
                f"{_oauth_state_token(second_payload['authorizationUrl'])}"
            ),
            "flowId": second_payload["flowId"],
        },
    )
    assert second_response.status_code == 200
    assert second_response.json() == {"status": "success", "errorMessage": None}

    first_status = await async_client.get("/api/oauth/status", params={"flowId": first_payload["flowId"]})
    assert first_status.status_code == 200
    assert first_status.json() == {"status": "success", "errorMessage": None}

    second_status = await async_client.get("/api/oauth/status", params={"flowId": second_payload["flowId"]})
    assert second_status.status_code == 200
    assert second_status.json() == {"status": "success", "errorMessage": None}

    accounts = await async_client.get("/api/accounts")
    assert accounts.status_code == 200
    data = accounts.json()["accounts"]
    expected_ids = {
        generate_unique_account_id("acc_code-first", "code-first@example.com"),
        generate_unique_account_id("acc_code-second", "code-second@example.com"),
    }
    assert expected_ids.issubset({account["accountId"] for account in data})


@pytest.mark.asyncio
async def test_callback_server_idle_stop_releases_store_lock_before_cleanup():
    async with SessionLocal() as session:
        service = oauth_module.OauthService(AccountsRepository(session))

        class ObservingCallbackServer:
            async def stop(self) -> None:
                assert not oauth_module._OAUTH_STORE.lock.locked()

        async with oauth_module._OAUTH_STORE.lock:
            flow = oauth_module.OAuthState(
                flow_id="finished-browser-flow",
                status="success",
                method="browser",
                state_token="finished-state",
                code_verifier="finished-verifier",
            )
            oauth_module._OAUTH_STORE.remember_flow_locked(flow)
            oauth_module._OAUTH_STORE.set_flow_status_locked(flow, status="success", error_message=None)
            oauth_module._OAUTH_STORE._callback_server = cast(
                oauth_module.OAuthCallbackServer,
                ObservingCallbackServer(),
            )

        await service._stop_callback_server_if_idle()


@pytest.mark.asyncio
async def test_existing_account_cleanup_releases_store_lock_before_callback_server_stop():
    class ExistingAccountRepo:
        async def list_accounts(self):
            return [object()]

    class ObservingCallbackServer:
        async def stop(self) -> None:
            assert not oauth_module._OAUTH_STORE.lock.locked()

    service = oauth_module.OauthService(cast(AccountsRepository, ExistingAccountRepo()))
    async with oauth_module._OAUTH_STORE.lock:
        flow = oauth_module.OAuthState(
            flow_id="pending-browser-flow",
            status="pending",
            method="browser",
            state_token="pending-state",
            code_verifier="pending-verifier",
        )
        oauth_module._OAUTH_STORE.remember_flow_locked(flow)
        oauth_module._OAUTH_STORE._callback_server = cast(
            oauth_module.OAuthCallbackServer,
            ObservingCallbackServer(),
        )

    response = await service.start_oauth(oauth_module.OauthStartRequest())

    assert response.method == "browser"


@pytest.mark.asyncio
async def test_new_browser_flow_waits_for_stopping_callback_server_before_reusing_slot(monkeypatch):
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()
    started_servers: list[object] = []

    class StoppingCallbackServer:
        async def stop(self) -> None:
            stop_started.set()
            await release_stop.wait()

    class ReplacementCallbackServer:
        def __init__(self, *_, **__) -> None:
            self.started = False

        async def start(self) -> None:
            self.started = True
            started_servers.append(self)

        async def stop(self) -> None:
            return None

    async with SessionLocal() as session:
        service = oauth_module.OauthService(AccountsRepository(session))
        stopping_server = StoppingCallbackServer()
        async with oauth_module._OAUTH_STORE.lock:
            oauth_module._OAUTH_STORE._callback_server = cast(oauth_module.OAuthCallbackServer, stopping_server)

        monkeypatch.setattr(oauth_module, "OAuthCallbackServer", ReplacementCallbackServer)
        stop_task = asyncio.create_task(service._stop_callback_server_if_idle())
        await stop_started.wait()

        start_task = asyncio.create_task(service._start_browser_flow())
        await asyncio.sleep(0)
        release_stop.set()

        response = await asyncio.wait_for(start_task, timeout=1)
        await stop_task

        assert response.method == "browser"
        assert len(started_servers) == 1
        async with oauth_module._OAUTH_STORE.lock:
            assert oauth_module._OAUTH_STORE._callback_server is started_servers[0]


@pytest.mark.asyncio
async def test_manual_callback_idempotent_success_requires_requested_flow(async_client, monkeypatch):
    async def fake_callback_server_start(self) -> None:
        return None

    exchange_calls: list[str] = []

    async def fake_exchange_authorization_code(**kwargs):
        code = kwargs["code"]
        exchange_calls.append(code)
        return OAuthTokens(
            access_token=f"access-{code}",
            refresh_token=f"refresh-{code}",
            id_token=None,
            expires_in=3600,
            account_id=f"acc_{code}",
            email=f"{code}@example.com",
        )

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)
    monkeypatch.setattr(oauth_module, "exchange_authorization_code", fake_exchange_authorization_code)

    first_start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert first_start.status_code == 200
    first_payload = first_start.json()
    first_callback_url = (
        f"http://localhost:1455/auth/callback?code=code-first&state="
        f"{_oauth_state_token(first_payload['authorizationUrl'])}"
    )

    second_start = await async_client.post("/api/oauth/start", json={"forceMethod": "browser"})
    assert second_start.status_code == 200
    second_payload = second_start.json()
    assert second_payload["flowId"] != first_payload["flowId"]

    first_response = await async_client.post(
        "/api/oauth/manual-callback",
        json={
            "callbackUrl": first_callback_url,
            "flowId": first_payload["flowId"],
        },
    )
    assert first_response.status_code == 200
    assert first_response.json() == {"status": "success", "errorMessage": None}

    replay_with_wrong_flow = await async_client.post(
        "/api/oauth/manual-callback",
        json={
            "callbackUrl": first_callback_url,
            "flowId": second_payload["flowId"],
        },
    )
    assert replay_with_wrong_flow.status_code == 200
    assert replay_with_wrong_flow.json() == {
        "status": "error",
        "errorMessage": "Invalid OAuth callback: state mismatch or missing code.",
    }
    assert exchange_calls == ["code-first"]

    first_status = await async_client.get("/api/oauth/status", params={"flowId": first_payload["flowId"]})
    assert first_status.status_code == 200
    assert first_status.json() == {"status": "success", "errorMessage": None}

    second_status = await async_client.get("/api/oauth/status", params={"flowId": second_payload["flowId"]})
    assert second_status.status_code == 200
    assert second_status.json() == {"status": "pending", "errorMessage": None}


def _make_replica_service(store: "oauth_module.OAuthStateStore") -> oauth_module.OauthService:
    """Build an OauthService bound to a distinct process-local store, standing in
    for one replica behind a load balancer. All replicas share the one test DB."""

    from contextlib import asynccontextmanager

    _injected_oauth_stores.append(store)

    @asynccontextmanager
    async def _repo_factory():
        async with SessionLocal() as session:
            yield AccountsRepository(session)

    return oauth_module.OauthService(
        cast(AccountsRepository, SimpleNamespace(list_accounts=AsyncMock(return_value=[]))),
        repo_factory=_repo_factory,
        store=store,
    )


@pytest.mark.asyncio
async def test_browser_oauth_flow_completes_on_replica_that_did_not_start_it(monkeypatch):
    """Multi-replica regression: replica A starts a browser flow; the manually
    pasted callback lands on replica B, whose in-memory store never saw the flow.

    Before this change the flow record lived only in replica A's process-local
    ``_OAUTH_STORE``, so replica B reported "state mismatch" and the account was
    never added. Now B loads the encrypted verifier + metadata from the shared
    DB and completes the exchange.
    """

    async def fake_callback_server_start(self) -> None:
        return None

    email = "cross-replica@example.com"
    raw_account_id = "acc_cross_replica"

    async def fake_exchange_authorization_code(**_):
        return OAuthTokens(
            access_token="cross-access",
            refresh_token="cross-refresh",
            id_token=None,
            expires_in=3600,
            organization_id="org_cross_replica",
            account_id=raw_account_id,
            email=email,
        )

    async def fake_oauth_route():
        return None

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)
    monkeypatch.setattr(oauth_module, "exchange_authorization_code", fake_exchange_authorization_code)
    monkeypatch.setattr(oauth_module, "_oauth_route", fake_oauth_route)

    replica_a = _make_replica_service(oauth_module.OAuthStateStore())
    replica_b = _make_replica_service(oauth_module.OAuthStateStore())

    start = await replica_a.start_oauth(oauth_module.OauthStartRequest(force_method="browser"))
    assert start.method == "browser"
    assert start.flow_id is not None
    state_token = _oauth_state_token(start.authorization_url or "")

    # Replica B never held this flow in memory.
    async with replica_b._store.lock:
        assert replica_b._store.get_flow_by_state_token_locked(state_token) is None

    result = await replica_b.manual_callback(
        f"http://localhost:1455/auth/callback?code=cross-code&state={state_token}",
        flow_id=start.flow_id,
    )
    assert result.status == "success"

    expected_account_id = generate_unique_account_id(raw_account_id, email)
    async with SessionLocal() as session:
        stored = await AccountsRepository(session).get_by_id(expected_account_id)
    assert stored is not None
    assert stored.pool_class == "oauth_seat"
    assert stored.anthropic_account_id == raw_account_id
    assert stored.anthropic_organization_id == "org_cross_replica"
    assert stored.token_expires_at is not None

    # Replica A, still holding a stale in-memory pending flow, must report the
    # authoritative success written by replica B to the shared DB.
    status_a = await replica_a.oauth_status(start.flow_id)
    assert status_a.status == "success"


@pytest.mark.asyncio
async def test_oauth_status_reads_completion_written_by_another_replica(monkeypatch):
    """A flow started on replica A and marked success in the shared DB by another
    replica is reported as success by A's status poll, not its stale pending."""

    async def fake_callback_server_start(self) -> None:
        return None

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)

    replica_a = _make_replica_service(oauth_module.OAuthStateStore())
    start = await replica_a.start_oauth(oauth_module.OauthStartRequest(force_method="browser"))
    assert start.flow_id is not None

    # Replica A's in-memory view is still pending.
    async with replica_a._store.lock:
        local = replica_a._store.get_flow_locked(start.flow_id)
        assert local is not None and local.status == "pending"

    # Another replica writes the terminal status directly to the shared DB.
    async with SessionLocal() as session:
        repo = oauth_module.OAuthFlowRepository(session, TokenEncryptor())
        assert await repo.set_status(start.flow_id, status="success", error_message=None)

    status = await replica_a.oauth_status(start.flow_id)
    assert status.status == "success"


def test_is_expired_pending_normalizes_tz_aware_expiry():
    """Finding 1 (dialect-agnostic): on PostgreSQL asyncpg returns an
    offset-AWARE ``expires_at`` for the ``DateTime(timezone=True)`` column while
    ``utcnow()`` is naive UTC. The expiry comparison must normalize before
    comparing instead of raising ``TypeError: can't compare offset-naive and
    offset-aware datetimes``.
    """

    from app.modules.oauth.repository import OAuthFlowRepository

    now = utcnow()  # naive UTC, as returned by ``utcnow``
    live_aware = cast(
        OAuthFlowState,
        SimpleNamespace(status="pending", expires_at=datetime.now(timezone.utc) + timedelta(hours=1)),
    )
    expired_aware = cast(
        OAuthFlowState,
        SimpleNamespace(status="pending", expires_at=datetime.now(timezone.utc) - timedelta(hours=1)),
    )

    # Must not raise, and must classify correctly.
    assert OAuthFlowRepository._is_expired_pending(live_aware, now) is False
    assert OAuthFlowRepository._is_expired_pending(expired_aware, now) is True


@pytest.mark.asyncio
async def test_get_by_flow_id_survives_tz_aware_expiry_from_asyncpg():
    """Finding 1 (read path): ``get_by_flow_id`` / ``get_by_state_token`` must
    not raise when the ORM row carries an offset-aware ``expires_at`` (asyncpg's
    representation for ``DateTime(timezone=True)`` on PostgreSQL) and must still
    correctly classify live vs expired pending flows.
    """

    encryptor = TokenEncryptor()
    async with SessionLocal() as session:
        repo = oauth_module.OAuthFlowRepository(session, encryptor)
        await repo.create(
            oauth_module.OAuthFlowRecord(
                flow_id="tz-aware-flow",
                method="browser",
                status="pending",
                state_token="tz-aware-state",
                code_verifier="tz-aware-verifier",
                expires_at=utcnow() + timedelta(hours=1),
            )
        )

    async with SessionLocal() as session:
        repo = oauth_module.OAuthFlowRepository(session, encryptor)
        row = await session.get(OAuthFlowState, "tz-aware-flow")
        assert row is not None

        # Simulate asyncpg: replace the naive value with an offset-aware one.
        row.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        live = await repo.get_by_flow_id("tz-aware-flow")
        assert live is not None
        live_by_state = await repo.get_by_state_token("tz-aware-state")
        assert live_by_state is not None

        # Aware + expired must be filtered out, still without raising.
        row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        assert await repo.get_by_flow_id("tz-aware-flow") is None
        assert await repo.get_by_state_token("tz-aware-state") is None


@pytest.mark.asyncio
async def test_complete_on_origin_replica_honors_durable_success_from_other_replica(monkeypatch):
    """Finding 2: replica A starts a browser flow; replica B completes it and
    writes durable success to the shared DB. The dashboard on A polls status
    (success) then immediately calls ``/complete`` with the same ``flowId``.

    Before the fix, ``complete_oauth`` used A's stale local ``pending`` browser
    flow (hydration skips existing flows) and returned ``pending``, so the UI
    flipped back to pending and never invalidated accounts. Now the durable
    terminal status is authoritative: ``/complete`` returns success and A's
    in-memory flow is reconciled.
    """

    async def fake_callback_server_start(self) -> None:
        return None

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)

    replica_a = _make_replica_service(oauth_module.OAuthStateStore())
    start = await replica_a.start_oauth(oauth_module.OauthStartRequest(force_method="browser"))
    assert start.flow_id is not None

    # Replica A's in-memory flow is still pending.
    async with replica_a._store.lock:
        local = replica_a._store.get_flow_locked(start.flow_id)
        assert local is not None and local.status == "pending"

    # Another replica completes the flow and writes durable success.
    async with SessionLocal() as session:
        repo = oauth_module.OAuthFlowRepository(session, TokenEncryptor())
        assert await repo.set_status(start.flow_id, status="success", error_message=None)

    # poll(): status returns the durable success ...
    status = await replica_a.oauth_status(start.flow_id)
    assert status.status == "success"

    # ... then the frontend immediately calls /complete with the same flowId.
    complete = await replica_a.complete_oauth(oauth_module.OauthCompleteRequest(flow_id=start.flow_id))
    assert complete.status == "success"

    # The origin replica's in-memory flow converges on the durable terminal.
    async with replica_a._store.lock:
        local = replica_a._store.get_flow_locked(start.flow_id)
        assert local is not None and local.status == "success"


@pytest.mark.asyncio
async def test_set_status_success_is_not_overwritten_by_later_error():
    """Monotonic terminal write (repository.py): when a device flow is completed
    twice (duplicate/losing poller), the poller that exchanged the code persists
    ``success`` while the other later receives an OAuth error for the consumed
    code. That stale error MUST NOT turn the durable row back to ``error``.
    """

    encryptor = TokenEncryptor()
    async with SessionLocal() as session:
        repo = oauth_module.OAuthFlowRepository(session, encryptor)
        await repo.create(
            oauth_module.OAuthFlowRecord(
                flow_id="mono-flow",
                method="browser",
                status="pending",
                state_token="mono-state",
                code_verifier="mono-verifier",
                expires_at=utcnow() + timedelta(hours=1),
            )
        )

    async with SessionLocal() as session:
        repo = oauth_module.OAuthFlowRepository(session, encryptor)
        # Winning poller persists success.
        assert await repo.set_status("mono-flow", status="success", error_message=None) is True
        # Losing/duplicate poller's later error for the consumed code is rejected.
        assert await repo.set_status("mono-flow", status="error", error_message="invalid_grant") is False
        record = await repo.get_by_flow_id("mono-flow")
        assert record is not None
        assert record.status == "success"
        assert record.error_message is None
        # success -> success remains idempotent.
        assert await repo.set_status("mono-flow", status="success", error_message=None) is True
        # error -> success is still allowed (success may win over an earlier error).
        await repo.create(
            oauth_module.OAuthFlowRecord(
                flow_id="err-then-ok",
                method="browser",
                status="error",
                error_message="transient",
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        assert await repo.set_status("err-then-ok", status="success", error_message=None) is True
        healed = await repo.get_by_flow_id("err-then-ok")
        assert healed is not None and healed.status == "success"


@pytest.mark.asyncio
async def test_set_status_success_is_atomic_across_concurrent_sessions():
    """The monotonic guard MUST hold under real cross-session concurrency, not
    only single-session Python logic. Two pollers (separate DB sessions) both
    read the row while it is ``pending`` (the TOCTOU window); the winning poller
    commits ``success`` first, then the losing poller tries to write ``error``
    for the now-consumed device code. A client-side read-then-write guard would
    still see its stale ``pending`` snapshot and clobber the success; the SQL
    conditional UPDATE rejects it atomically.
    """

    encryptor = TokenEncryptor()
    async with SessionLocal() as seed_session:
        await oauth_module.OAuthFlowRepository(seed_session, encryptor).create(
            oauth_module.OAuthFlowRecord(
                flow_id="race-flow",
                method="browser",
                status="pending",
                state_token="race-state",
                code_verifier="race-verifier",
                expires_at=utcnow() + timedelta(hours=1),
            )
        )

    async with SessionLocal() as session_win, SessionLocal() as session_lose:
        repo_win = oauth_module.OAuthFlowRepository(session_win, encryptor)
        repo_lose = oauth_module.OAuthFlowRepository(session_lose, encryptor)

        # Losing poller loads the still-``pending`` row into its own session and
        # keeps that transaction/snapshot open — the TOCTOU window where both
        # transactions have observed ``pending``. A client-side read-then-write
        # guard would re-use this cached ``pending`` snapshot on its own status
        # write and wrongly conclude the downgrade is allowed.
        preloaded = await session_lose.get(OAuthFlowState, "race-flow")
        assert preloaded is not None and preloaded.status == "pending"

        # Winning poller exchanges the code and commits ``success`` first.
        assert await repo_win.set_status("race-flow", status="success", error_message=None) is True

        # Losing poller now gets an OAuth error for the consumed code while still
        # holding its stale ``pending`` snapshot. The atomic conditional UPDATE
        # re-checks the current row state in SQL and refuses to downgrade the
        # committed ``success``; a client-side guard would clobber it.
        applied = await repo_lose.set_status("race-flow", status="error", error_message="invalid_grant")
        assert applied is False

    async with SessionLocal() as verify_session:
        record = await oauth_module.OAuthFlowRepository(verify_session, encryptor).get_by_flow_id("race-flow")
        assert record is not None
        assert record.status == "success"
        assert record.error_message is None


@pytest.mark.asyncio
async def test_expired_local_browser_flow_callback_is_rejected_on_origin_replica(monkeypatch):
    """The pending-flow TTL must hold uniformly, including on the replica that
    started the flow and still holds its local state: a callback that arrives
    after the TTL is rejected (state-mismatch / expired) instead of being
    completed from the stale cached verifier.
    """

    async def fake_callback_server_start(self) -> None:
        return None

    exchange_calls: list[str | None] = []

    async def fake_exchange_authorization_code(**kwargs):
        exchange_calls.append(kwargs.get("code"))
        raise AssertionError("an expired flow must never exchange the authorization code")

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)
    monkeypatch.setattr(oauth_module, "exchange_authorization_code", fake_exchange_authorization_code)

    replica = _make_replica_service(oauth_module.OAuthStateStore())
    start = await replica.start_oauth(oauth_module.OauthStartRequest(force_method="browser"))
    assert start.flow_id is not None
    state_token = _oauth_state_token(start.authorization_url or "")

    # Force the flow past its TTL both in the local store and the shared DB.
    async with replica._store.lock:
        local = replica._store.get_flow_by_state_token_locked(state_token)
        assert local is not None and local.status == "pending"
        local.expires_at = time.time() - 1
    async with SessionLocal() as session:
        row = await session.get(OAuthFlowState, start.flow_id)
        assert row is not None
        row.expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()

    result = await replica.manual_callback(
        f"http://localhost:1455/auth/callback?code=expired-code&state={state_token}",
        flow_id=start.flow_id,
    )

    assert result.status == "error"
    assert result.error_message == "Invalid OAuth callback: state mismatch or missing code."
    # The stale verifier was never used to exchange the code.
    assert exchange_calls == []


@pytest.mark.parametrize("entry_point", ["status", "complete", "manual_callback", "handle_callback"])
@pytest.mark.asyncio
async def test_entry_points_honor_durable_terminal_over_local_pending(monkeypatch, entry_point):
    """Root-consolidation regression: EVERY entry point that resolves a flow from
    local state must consult the DB-authoritative status first, so a durable
    terminal written by another replica wins over this replica's stale local
    ``pending`` -- and a consumed authorization code is never replayed.
    """

    from aiohttp.test_utils import make_mocked_request

    async def fake_callback_server_start(self) -> None:
        return None

    async def fake_oauth_route():
        return None

    exchange_calls: list[str | None] = []

    async def fake_exchange_authorization_code(**kwargs):
        exchange_calls.append(kwargs.get("code"))
        raise AssertionError("a durable-terminal flow must never re-exchange the code")

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)
    monkeypatch.setattr(oauth_module, "_oauth_route", fake_oauth_route)
    monkeypatch.setattr(oauth_module, "exchange_authorization_code", fake_exchange_authorization_code)

    replica = _make_replica_service(oauth_module.OAuthStateStore())
    start = await replica.start_oauth(oauth_module.OauthStartRequest(force_method="browser"))
    assert start.flow_id is not None
    state_token = _oauth_state_token(start.authorization_url or "")

    # Origin replica still holds the flow as pending in memory.
    async with replica._store.lock:
        local = replica._store.get_flow_locked(start.flow_id)
        assert local is not None and local.status == "pending"

    # Another replica writes the durable terminal success.
    async with SessionLocal() as session:
        assert await OAuthFlowRepository(session, TokenEncryptor()).set_status(
            start.flow_id, status="success", error_message=None
        )

    if entry_point == "status":
        resp = await replica.oauth_status(start.flow_id)
        assert resp.status == "success"
    elif entry_point == "complete":
        resp = await replica.complete_oauth(oauth_module.OauthCompleteRequest(flow_id=start.flow_id))
        assert resp.status == "success"
    elif entry_point == "manual_callback":
        resp = await replica.manual_callback(
            f"http://localhost:1455/auth/callback?code=replay-code&state={state_token}",
            flow_id=start.flow_id,
        )
        assert resp.status == "success"
    elif entry_point == "handle_callback":
        request = make_mocked_request("GET", f"/auth/callback?code=replay-code&state={state_token}")
        response = await replica._handle_callback(request)
        assert response.status == 200
        assert response.text is not None and "Login failed" not in response.text

    # The origin replica's in-memory flow is reconciled to the durable terminal.
    async with replica._store.lock:
        local = replica._store.get_flow_locked(start.flow_id)
        assert local is not None and local.status == "success"

    # The consumed authorization code was never re-exchanged.
    assert exchange_calls == []


@pytest.mark.asyncio
async def test_browser_callback_replay_on_origin_does_not_reexchange_consumed_code(monkeypatch):
    """The reported callback-replay class: replica A starts a browser flow (local
    pending); another replica completes it (durable success). A second browser
    redirect / pasted callback for the same state lands back on A. A must observe
    the durable success instead of reusing the already-consumed authorization
    code (which upstream would reject, surfacing a spurious error to the user).
    """

    from aiohttp.test_utils import make_mocked_request

    async def fake_callback_server_start(self) -> None:
        return None

    async def fake_oauth_route():
        return None

    exchange_calls: list[str | None] = []

    async def fake_exchange_authorization_code(**kwargs):
        exchange_calls.append(kwargs.get("code"))
        raise AssertionError("replayed callback must not re-exchange the consumed code")

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)
    monkeypatch.setattr(oauth_module, "_oauth_route", fake_oauth_route)
    monkeypatch.setattr(oauth_module, "exchange_authorization_code", fake_exchange_authorization_code)

    replica_a = _make_replica_service(oauth_module.OAuthStateStore())
    start = await replica_a.start_oauth(oauth_module.OauthStartRequest(force_method="browser"))
    assert start.flow_id is not None
    state_token = _oauth_state_token(start.authorization_url or "")

    # Another replica completed the flow: durable success in the shared DB.
    async with SessionLocal() as session:
        assert await OAuthFlowRepository(session, TokenEncryptor()).set_status(
            start.flow_id, status="success", error_message=None
        )

    # Replayed pasted callback on the origin replica.
    manual = await replica_a.manual_callback(
        f"http://localhost:1455/auth/callback?code=consumed-code&state={state_token}",
        flow_id=start.flow_id,
    )
    assert manual.status == "success"

    # Replayed browser redirect on the origin replica.
    request = make_mocked_request("GET", f"/auth/callback?code=consumed-code&state={state_token}")
    response = await replica_a._handle_callback(request)
    assert response.status == 200
    assert response.text is not None and "Login failed" not in response.text

    assert exchange_calls == []


@pytest.mark.parametrize("path", ["manual_callback", "handle_callback"])
@pytest.mark.asyncio
async def test_loser_browser_callback_honors_durable_success_not_error(monkeypatch, path):
    """Browser-callback analog of the loser-writes-error bug: two callbacks race
    on the same single-use authorization code. The winner commits durable success
    WHILE the loser is exchanging; the loser's exchange then fails with
    ``invalid_grant``. The loser's terminal ERROR write is rejected by the
    monotonic guard (durable row already ``success``), so the loser MUST report
    the durable SUCCESS (not error) and MUST NOT leave the local flow in error.
    """

    from aiohttp.test_utils import make_mocked_request

    holder: dict[str, str] = {}

    async def fake_callback_server_start(self) -> None:
        return None

    async def fake_oauth_route():
        return None

    async def fake_exchange_authorization_code(**_kwargs):
        # The winner commits durable success DURING the loser's exchange (so the
        # top-of-callback reconciliation gate saw ``pending`` and the loser
        # actually reaches this exchange), then the loser's exchange of the
        # now-consumed code fails.
        async with SessionLocal() as session:
            await OAuthFlowRepository(session, TokenEncryptor()).set_status(
                holder["flow_id"], status="success", error_message=None
            )
        raise OAuthError("invalid_grant", "Authorization code expired", status_code=400)

    monkeypatch.setattr(oauth_module.OAuthCallbackServer, "start", fake_callback_server_start)
    monkeypatch.setattr(oauth_module, "_oauth_route", fake_oauth_route)
    monkeypatch.setattr(oauth_module, "exchange_authorization_code", fake_exchange_authorization_code)

    replica = _make_replica_service(oauth_module.OAuthStateStore())
    start = await replica.start_oauth(oauth_module.OauthStartRequest(force_method="browser"))
    assert start.flow_id is not None
    holder["flow_id"] = start.flow_id
    state_token = _oauth_state_token(start.authorization_url or "")

    if path == "manual_callback":
        manual = await replica.manual_callback(
            f"http://localhost:1455/auth/callback?code=consumed-code&state={state_token}",
            flow_id=start.flow_id,
        )
        assert manual.status == "success"
    else:
        request = make_mocked_request("GET", f"/auth/callback?code=consumed-code&state={state_token}")
        response = await replica._handle_callback(request)
        assert response.status == 200
        assert response.text is not None and "Login failed" not in response.text

    # The loser must not leave the local flow in error; it honors durable success.
    async with replica._store.lock:
        local = replica._store.get_flow_locked(start.flow_id)
        assert local is not None and local.status == "success"
    async with SessionLocal() as session:
        record = await OAuthFlowRepository(session, TokenEncryptor()).get_by_flow_id(start.flow_id)
    assert record is not None and record.status == "success"
