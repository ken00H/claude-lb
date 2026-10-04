from __future__ import annotations

import base64
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.core.balancer import AccountState, select_account
from app.core.crypto import TokenEncryptor
from app.db.models import Account, AccountStatus
from app.modules.proxy._service.http_bridge.helpers import _http_bridge_session_account_active
from app.modules.proxy.account_eligibility import stored_access_token_expires_at
from app.modules.proxy.load_balancer import _build_states

pytestmark = pytest.mark.unit


def _jwt(*, expires_at: int) -> str:
    def encode(payload: dict[str, object]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode({'exp': expires_at})}."


def test_stored_access_token_expiry_is_derived_from_encrypted_jwt() -> None:
    encryptor = TokenEncryptor()

    expires_at = stored_access_token_expires_at(
        encryptor.encrypt(_jwt(expires_at=1_700_000_123)),
        encryptor,
    )

    assert expires_at == 1_700_000_123.0


def test_build_states_carries_reauth_access_token_expiry() -> None:
    encryptor = TokenEncryptor()
    account = Account(
        id="reauth",
        chatgpt_account_id="chatgpt-reauth",
        email="reauth@example.com",
        plan_type="plus",
        access_token_encrypted=encryptor.encrypt(_jwt(expires_at=1_700_000_123)),
        refresh_token_encrypted=encryptor.encrypt("refresh"),
        id_token_encrypted=encryptor.encrypt("id"),
        last_refresh=datetime(2026, 1, 1),
        status=AccountStatus.REAUTH_REQUIRED,
    )

    states, _ = _build_states(
        accounts=[account],
        latest_primary={},
        latest_secondary={},
        latest_monthly={},
        runtime={},
        encryptor=encryptor,
    )

    assert states[0].access_token_expires_at == 1_700_000_123.0


def test_selection_skips_expired_reauth_account() -> None:
    now = 1_700_000_000.0
    states = [
        AccountState(
            "expired",
            AccountStatus.REAUTH_REQUIRED,
            used_percent=1.0,
            access_token_expires_at=now,
        ),
        AccountState("active", AccountStatus.ACTIVE, used_percent=50.0),
    ]

    result = select_account(states, now=now, routing_strategy="usage_weighted")

    assert result.account is not None
    assert result.account.account_id == "active"


def test_all_expired_reauth_accounts_report_reauthentication() -> None:
    now = 1_700_000_000.0
    states = [
        AccountState(
            "expired-a",
            AccountStatus.REAUTH_REQUIRED,
            access_token_expires_at=now - 1,
        ),
        AccountState(
            "expired-b",
            AccountStatus.REAUTH_REQUIRED,
            access_token_expires_at=now,
        ),
    ]

    result = select_account(states, now=now)

    assert result.account is None
    assert result.error_message == "All accounts require re-authentication"


def test_http_bridge_rejects_expired_reauth_session() -> None:
    session = SimpleNamespace(
        account=SimpleNamespace(
            id="expired-bridge-owner",
            status=AccountStatus.REAUTH_REQUIRED,
        ),
        access_token_expires_at=0.0,
    )

    assert not _http_bridge_session_account_active(cast(Any, session))


class _StubModelRegistry:
    """Minimal registry seam: models absent from the map have no catalog
    entry (``plan_types_for_model`` returns None)."""

    def __init__(self, plans_by_model: dict[str, frozenset[str]]) -> None:
        self._plans_by_model = plans_by_model

    def plan_types_for_model(self, model: str) -> frozenset[str] | None:
        return self._plans_by_model.get(model)


def _plan_candidate(account_id: str, plan_type: str | None) -> Account:
    return Account(
        id=account_id,
        pool_class="oauth_seat",
        email=f"{account_id}@example.com",
        plan_type=plan_type,
        access_token_encrypted=b"access",
        refresh_token_encrypted=b"refresh",
        last_refresh=datetime(2026, 1, 1),
        status=AccountStatus.ACTIVE,
    )


def test_unknown_plan_is_routable_only_without_catalog_plan_restrictions() -> None:
    """Current pinned semantics (see add-anthropic-account-model): an
    ``unknown``-plan OAuth seat routes when the model has no catalog entry
    (no allowed-plans restriction), but is excluded when the catalog lists
    allowed plans that do not literally include ``unknown``. Aligning the
    filter with the spec delta's "unknown is routable" requirement is a
    deliberately deferred decision recorded in the change notes."""
    from app.modules.proxy._load_balancer.model_eligibility import _filter_accounts_for_model

    registry = _StubModelRegistry({"catalog-model": frozenset({"plus", "team"})})
    unknown_seat = _plan_candidate("unknown-seat", "unknown")
    plus_seat = _plan_candidate("plus-seat", "plus")

    uncatalogued = _filter_accounts_for_model(
        [unknown_seat, plus_seat],
        "model-not-in-catalog",
        registry=cast(Any, registry),
    )
    assert [account.id for account in uncatalogued] == ["unknown-seat", "plus-seat"]

    catalogued = _filter_accounts_for_model(
        [unknown_seat, plus_seat],
        "catalog-model",
        registry=cast(Any, registry),
    )
    assert [account.id for account in catalogued] == ["plus-seat"]

    explicitly_allowed = _StubModelRegistry({"catalog-model": frozenset({"unknown"})})
    admitted = _filter_accounts_for_model(
        [unknown_seat],
        "catalog-model",
        registry=cast(Any, explicitly_allowed),
    )
    assert [account.id for account in admitted] == ["unknown-seat"]
