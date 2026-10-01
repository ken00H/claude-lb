from __future__ import annotations

import base64
import hashlib
import urllib.parse

import pytest

from app.core.clients.oauth import build_authorization_url, pkce_challenge, split_pasted_callback

pytestmark = pytest.mark.unit


def test_pkce_challenge_matches_sha256():
    verifier = "test_verifier"
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert pkce_challenge(verifier) == expected


def test_build_authorization_url_contains_required_params():
    url = build_authorization_url(
        state="state_123",
        code_challenge="challenge_456",
        authorize_url="https://claude.ai/oauth/authorize",
        client_id="client_id",
        redirect_uri="https://console.anthropic.com/oauth/code/callback",
        scope="user:inference",
    )

    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "claude.ai"
    assert parsed.path == "/oauth/authorize"

    query = urllib.parse.parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client_id"]
    assert query["redirect_uri"] == ["https://console.anthropic.com/oauth/code/callback"]
    assert query["scope"] == ["user:inference"]
    assert query["code_challenge"] == ["challenge_456"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["state_123"]
    # Copy/paste callback mode: the browser shows the code instead of calling
    # a local redirect server.
    assert query["code"] == ["true"]


def test_split_pasted_callback_code_state():
    assert split_pasted_callback("abc#def") == ("abc", "def")


def test_split_pasted_callback_bare_code():
    assert split_pasted_callback("abc") == ("abc", None)


def test_split_pasted_callback_strips_whitespace():
    assert split_pasted_callback("  abc#def  ") == ("abc", "def")
