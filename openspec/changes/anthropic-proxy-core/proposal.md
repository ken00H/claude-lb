# Proposal: anthropic-proxy-core

## Why

The Anthropic adaptation roadmap (umbrella change `anthropic-upstream-adaptation`, increment 3) has no
downstream proxy surface: claude-lb currently manages Anthropic accounts but cannot serve traffic. This
change lands the smallest complete increment-3 slice so an Anthropic account pool — including a free
claude.ai OAuth seat — can serve real `/v1/messages` traffic end-to-end.

## What Changes

- New `POST /v1/messages` route (Anthropic Messages API) with validation, request-id handling, SSE
  streaming passthrough, and non-streaming responses, guarded by the existing proxy API-key auth.
- New Anthropic upstream client (`app/core/clients/anthropic.py`) targeting `https://api.anthropic.com`,
  with per-pool-class credential headers (`oauth_seat` → Bearer + Claude Code client impersonation;
  `api_key` → `x-api-key`) and Anthropic-native error envelopes.
- Wire the existing balancer, account leases, API-key reservations, and failover loop to the new route:
  excluded accounts leave the selection loop, reservations settle before error-health writes, owner
  invariants preserved by not introducing new affinity sources (the Messages API is stateless).
- Token freshness for opaque Anthropic tokens: `oauth_seat` accounts refresh on `token_expires_at`
  proximity (not the 8-day ChatGPT age heuristic), and eligibility reads the stored `token_expires_at`
  column for `oauth_seat` rows.
- Rate-limit response headers (`anthropic-ratelimit-*` and any subscription limit headers) are recorded
  where existing fields fit; no window capacity constants (umbrella context: no invented numbers).
- Route-layer tests with mocked Anthropic upstream endpoints.

## Capabilities

### New Capabilities
- `anthropic-proxy`: `/v1/messages` endpoint, load balancing, per-pool-class request authentication,
  streaming, failover invariants.

## Impact

- Code: `app/modules/proxy/` (new route + service slice), `app/core/clients/anthropic.py` (new),
  `app/core/config/settings.py` (protocol constants only), `app/modules/accounts/auth_manager.py`,
  `app/modules/proxy/account_eligibility.py`, `app/main.py` (router registration).
- Explicitly deferred (stay on the umbrella): 5-hour/weekly window capacities (3.3 constants, blocked on
  live Pro/Max evidence), Codex proxy core + OpenAI-compat layer removal (3.5), legacy column drops,
  usage rollup remapping, model-registry swap, dashboard rebrand (section 4).
- No new `CODEX_LB_*` settings; new constants are protocol constants (PRINCIPLES.md P2), not tunables.
- The bundled Codex proxy core remains inert reference code until 3.5.
