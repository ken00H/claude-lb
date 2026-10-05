# Design: anthropic-proxy-core

## Context

Increment 2 landed the Anthropic account model (`add-anthropic-account-model`): one `accounts` table
with `pool_class` ∈ {`oauth_seat`, `api_key`}, PKCE copy/paste OAuth, refresh via
`platform.claude.com/v1/oauth/token` (path unverified), API-key import. The codex-era proxy machinery is
inert reference code. The umbrella change (`anthropic-upstream-adaptation`) tasks 3.1–3.4 and 3.6 define
this slice; 3.3 constants and 3.5 removal stay on the umbrella.

## Decisions

### D1 — Downstream surface: Anthropic-native passthrough

`POST /v1/messages` accepts and forwards the Anthropic Messages API shape. No request translation and no
OpenAI-compat layer (umbrella context.md: "Anthropic upstream already is Anthropic-shaped"). Validation
uses a strict pydantic `MessagesRequest` (model, messages, max_tokens, stream, system, tools, metadata)
so malformed payloads fail closed with Anthropic error envelopes before any account is selected.

### D2 — Upstream base and transport

Upstream Messages endpoint is `https://api.anthropic.com/v1/messages`. The base URL is a class-level
protocol constant (`ANTHROPIC_API_BASE_URL`) in `app/core/config/settings.py` beside the OAuth
constants — not a `Settings` field (PRINCIPLES.md P2; no tier entry needed, `check_settings_tiers`
stays green). Transport reuses `CodexClient` (resolved upstream-proxy route, endpoint fallback,
TLS fail-closed) — it is provider-neutral despite its name. Egress goes through the configured upstream
proxy route resolver like OAuth traffic, because claude.ai/anthropic hosts are region-blocked on some
deployments.

### D3 — Per-pool-class request authentication

- `oauth_seat`: `Authorization: Bearer <sk-ant-oat01-…>` plus Claude Code client impersonation headers
  (the accepted-risk mitigation from `add-anthropic-account-model/design.md`: "present upstream traffic
  as the official client"). Impersonation constants (`anthropic-beta` OAuth beta header, claude-cli
  user-agent, client id) are pinned in `settings.py` beside the OAuth block and recorded here; the exact
  set is confirmed at the first live probe and flipped in one place if wrong.
- `api_key`: `x-api-key: <sk-ant-…>` plus `anthropic-version: 2023-06-01` (the only documented stable
  version header for the Messages API).

Header selection is a pure function of `pool_class`; no other account attribute changes the request.

### D4 — Error envelopes and failure classification

Downstream errors use the Anthropic envelope `{"type": "error", "error": {"type", "message"}}` — not the
OpenAI envelope used by the codex routers. Upstream failure classification maps onto the existing
balancer taxonomy: HTTP 429 and `overloaded_error` (529) → rate_limit/transient handling; 401 with
`authentication_error` → auth failure eligible for failover; 5xx → transient retry. Account-neutral
rejections (invalid model, malformed request) never mark account health.

### D5 — Selection, failover, and invariants (reuse, do not translate)

The slice reuses `LoadBalancer.select_account` + account leases + `excluded_account_ids` failover + the
reservation/settlement spine untouched. New affinity sources are NOT introduced: the Messages API is
stateless (no `previous_response_id`, no file pins), so a request may fail over to another account
before the first token is emitted without violating ownership; losing prompt-cache warmth across
accounts is performance-neutral, not a correctness break. Invariants kept:

- Reservations settle before error-health writes on every path, including mid-stream death
  (partial-failure ordering tested).
- Excluded accounts actually leave the selection loop (failover passes the growing exclusion set).
- No shared `AsyncSession` across concurrent attempts; spawned tasks are cancelled/awaited on failure.
- Failover is bounded (3 account attempts, matching `_STREAM_MAX_ACCOUNT_ATTEMPTS`).

### D6 — Token freshness for opaque Anthropic tokens

Anthropic access tokens (`sk-ant-oat01-…`) are opaque, not JWTs: JWT-claim expiry parsing returns None.
Two changes:

- `AuthManager.ensure_fresh` adds an expiry trigger for `oauth_seat`: refresh when
  `token_expires_at` is within a small safety margin of now (tokens last hours, not days; the 8-day
  ChatGPT age heuristic would send expired tokens). Margin is a module constant, not a setting.
- `account_eligibility.account_access_token_expires_at` reads `Account.token_expires_at` for
  `oauth_seat` rows (falling back to JWT parsing for other rows), so `reauth_required` seats expire out
  of routability correctly.

### D7 — Rate-limit recording without constants

Upstream response headers `anthropic-ratelimit-*-*` (API keys) and any subscription limit headers seen
on real traffic are recorded into existing usage/rate-limit fields where they fit, and logged at debug
for the evidence pass. No window capacities (5h/weekly) are modeled in this slice — umbrella task 3.3's
constants require live Pro/Max evidence (parked item 1.2). 429s ride the existing
`mark_rate_limit` machinery.

### D8 — Streaming passthrough

SSE bytes from upstream are forwarded in order with preserved terminations (`message_stop`, `error`
events). No buffered re-emission (umbrella context: compatibility break for Claude Code clients). The
startup-error probe pattern (peek first event so pre-stream upstream errors become real HTTP error
responses) is reused. Keepalives follow the existing inject helper.

## Risks / Trade-offs

- Impersonation headers are best-effort evidence until the live probe; wrong set fails upstream with
  4xx and the constant block flips in one place.
- Pooling consumer OAuth seats is against Anthropic's Feb 2026 ToS (accepted risk, recorded in the
  account-model change); the API-key pool is the compliant fallback.
- Free-tier limit behavior unknown until a live call; existing 429 machinery covers it coarsely.
- The new route lives in `app/modules/proxy/api.py` (large file) to reuse auth/envelope/reservation
  helpers; the service slice lands under `_service/anthropic/` with a new domain entry in the
  `check_proxy_architecture` import allowlist, keeping the existing domains untouched.

## Migration Plan

No DB migration. Additive code only. Codex routes keep serving (inert) until 3.5.

## Open Questions

- Live token endpoint path (`/v1/oauth/token` vs `/api/oauth/token`) — parked item 1.1, one-constant flip.
- Exact subscription limit header names — parked item 1.2 / first live call.
