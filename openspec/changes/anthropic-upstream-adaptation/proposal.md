# Proposal: anthropic-upstream-adaptation

## Why

claude-lb is a fork of Soju06/codex-lb (MIT) that reuses its load-balancing, account,
usage-tracking, and dashboard machinery for a different ecosystem: pooling **Claude
accounts** (claude.ai OAuth seats and Anthropic API keys) behind a single
**Anthropic-compatible** endpoint instead of pooling ChatGPT/Codex accounts behind a
Codex-compatible one. The fork scaffold (increment 1) rebranded the identity and removed
the Rust/codex-protocol egress; the Codex proxy core remains in-tree as inert reference
code until it is rewritten.

## What Changes

Four increments, each landing as its own openspec change (or a well-scoped slice of this
umbrella):

1. **Fork scaffold (done)** — clone, rebrand (package `claude-lb`, entrypoints
   `claude-lb` / `claude-lb-db`), delete Rust crates and cargo tooling, README stub with
   upstream attribution.
2. **Anthropic account model** — replace ChatGPT OAuth account machinery with:
   - claude.ai OAuth accounts (PKCE flow, token storage/refresh, seat identity),
   - Anthropic API key accounts as a second pool class,
   - DB schema deltas for account type, credentials, and plan/seat metadata.
3. **Proxy core rewrite** — `/v1/messages` (Anthropic Messages API) as the downstream
   surface: request translation is none (Anthropic-native passthrough shaping), account
   selection via the existing balancer, per-account 5-hour-window + weekly-cap rate
   tracking for subscription seats and `anthropic-ratelimit-*` header tracking for API
   keys, SSE streaming passthrough, failover/retry with codex-lb's ownership and
   settlement invariants preserved.
4. **Usage tracking + dashboard adaptation** — usage rollups keyed to Anthropic usage
   fields (`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
   `cache_creation_input_tokens`), dashboard rebrand, model registry swap to the
   Anthropic catalog.

## Capabilities

### New Capabilities
- `anthropic-accounts`: OAuth + API-key account pool, credential lifecycle, plan metadata.
- `anthropic-proxy`: `/v1/messages` endpoint, load balancing, rate-limit windows,
  streaming, failover invariants.

### Affected Capabilities (inherited from codex-lb, to be re-scoped per increment)
- `account-routing`, `api-keys`, `account-pool-usage-v1-usage`, `dashboard-*`,
  `database-migrations`, `configuration-tiers`.

## Impact

- Deletes Codex proxy/OpenAI-compat client code when increment 3 lands (kept until then
  as reference).
- `CODEX_LB_*` env-var prefix stays until a dedicated compatibility increment decides
  rename vs. alias (breaking-change decision — do not rename silently).
- `.github/` workflows and `flake.nix` still reference Rust builds and codex naming;
  must be fixed before the first push to a remote. Not touched in increment 1 (local-only).
- `docs/` still documents Codex behavior; docs sweep happens with increments 2–4.
