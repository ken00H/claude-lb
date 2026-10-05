"""Unit tests for the Anthropic proxy slice (openspec change anthropic-proxy-core)."""

from __future__ import annotations

import time
from typing import cast

import pytest
from pydantic import ValidationError

from app.core.anthropic.models import (
    MessagesRequest,
    anthropic_error,
    anthropic_validation_error,
    as_anthropic_error_envelope,
)
from app.core.auth.refresh import OAUTH_SEAT_EXPIRY_REFRESH_MARGIN_SECONDS, oauth_seat_token_near_expiry
from app.core.clients.anthropic import build_messages_headers
from app.core.config.settings import (
    ANTHROPIC_VERSION,
    CLAUDE_CODE_OAUTH_BETA,
)
from app.core.crypto import TokenEncryptor
from app.db.models import Account
from app.modules.proxy.account_eligibility import account_access_token_expires_at
from app.modules.proxy.helpers import classify_upstream_failure


class _FakeEncryptor:
    def decrypt(self, value: bytes) -> str:
        return value.decode("utf-8")


def test_oauth_seat_headers_use_bearer_and_impersonation():
    headers = build_messages_headers("oauth_seat", "sk-ant-oat01-test")

    assert headers["authorization"] == "Bearer sk-ant-oat01-test"
    assert headers["anthropic-version"] == ANTHROPIC_VERSION
    assert headers["anthropic-beta"] == CLAUDE_CODE_OAUTH_BETA
    assert "user-agent" in headers
    assert "x-api-key" not in headers


def test_api_key_headers_use_x_api_key_and_version():
    headers = build_messages_headers("api_key", "sk-ant-api-test")

    assert headers["x-api-key"] == "sk-ant-api-test"
    assert headers["anthropic-version"] == ANTHROPIC_VERSION
    assert "authorization" not in headers
    assert "anthropic-beta" not in headers


@pytest.mark.parametrize(
    ("error_code", "http_status", "expected"),
    [
        ("rate_limit_error", 429, "rate_limit"),
        ("api_error", 500, "retryable_transient"),
        ("overloaded_error", 529, "retryable_transient"),
        ("invalid_request_error", 400, "non_retryable"),
        ("permission_error", 403, "non_retryable"),
    ],
)
def test_classify_upstream_failure_recognizes_anthropic_types(error_code, http_status, expected):
    failure = classify_upstream_failure(
        error_code=error_code,
        error={"message": "test"},
        http_status=http_status,
        phase="first_event",
    )
    assert failure["failure_class"] == expected


def _account(**overrides) -> Account:
    defaults = dict(
        id="anth-unit",
        pool_class="oauth_seat",
        email="unit@example.com",
        plan_type="free",
        access_token_encrypted=b"access",
        refresh_token_encrypted=b"refresh",
        token_expires_at=None,
    )
    defaults.update(overrides)
    return Account(**defaults)


def test_oauth_seat_near_expiry_true_within_margin():
    margin = OAUTH_SEAT_EXPIRY_REFRESH_MARGIN_SECONDS
    account = _account(token_expires_at=int(time.time()) + margin // 2)
    assert oauth_seat_token_near_expiry(account) is True


def test_oauth_seat_far_from_expiry_not_refreshed():
    account = _account(token_expires_at=int(time.time()) + 6 * 3600)
    assert oauth_seat_token_near_expiry(account) is False


def test_non_oauth_seat_ignores_expiry_column():
    account = _account(pool_class="api_key", token_expires_at=int(time.time()) - 10)
    assert oauth_seat_token_near_expiry(account) is False


def test_oauth_seat_without_known_expiry_not_refreshed():
    account = _account(token_expires_at=None)
    assert oauth_seat_token_near_expiry(account) is False


def test_eligibility_reads_column_for_oauth_seat():
    expires = time.time() - 10
    account = _account(token_expires_at=int(expires))
    encryptor = cast(TokenEncryptor, _FakeEncryptor())
    assert account_access_token_expires_at(account, encryptor) == float(int(expires))


def test_eligibility_falls_back_to_claims_for_other_rows():
    import base64
    import json

    payload = {"exp": int((time.time() + 3600) * 1000)}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    account = _account(pool_class="chatgpt", access_token_encrypted=f"header.{body}.sig".encode())
    encryptor = cast(TokenEncryptor, _FakeEncryptor())
    assert account_access_token_expires_at(account, encryptor) is not None


def test_messages_request_validation_rejects_missing_max_tokens():
    with pytest.raises(ValidationError) as exc_info:
        MessagesRequest.model_validate({"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "hi"}]})
    envelope = anthropic_validation_error(exc_info.value)
    assert envelope["type"] == "error"
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["type"] == "invalid_request_error"


def test_messages_request_allows_extra_fields():
    payload = MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
            "system": "be brief",
            "temperature": 0.5,
        }
    )
    assert payload.to_payload()["temperature"] == 0.5
    assert payload.stream is None


def test_as_anthropic_envelope_passes_through_anthropic_shape():
    envelope = anthropic_error("rate_limit_error", "slow down")
    assert as_anthropic_error_envelope(envelope) == envelope


def test_as_anthropic_envelope_converts_openai_shape():
    converted = as_anthropic_error_envelope({"error": {"code": "no_accounts", "message": "No accounts"}})
    assert converted == {"type": "error", "error": {"type": "no_accounts", "message": "No accounts"}}
