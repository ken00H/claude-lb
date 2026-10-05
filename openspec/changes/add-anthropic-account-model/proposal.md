# Proposal: add-anthropic-account-model

## Why

claude-lb increments from `anthropic-upstream-adaptation` need an upstream account pool
with two classes: claude.ai OAuth seats (Claude Pro/Max subscriptions) and Anthropic API
keys. Today the fork still carries the ChatGPT account model end to end (OAuth flow,
identity columns, plan-capacity tables). This change replaces the account layer so
increments 3–4 (proxy rewrite, usage/dashboard) build on Anthropic semantics.

## What Changes

- **OAuth provider swap** — `app/core/clients/oauth.py` and `app/core/auth/` move from
  auth.openai.com constants to Anthropic's Claude Code OAuth surface:
  authorize `https://claude.ai/oauth/authorize`, token
  `https://console.anthropic.com/v1/oauth/token`, client_id
  `9d1c250a-e61b-44d9-88ed-5944d1962f5e`, PKCE S256, copy/paste callback
  (`code=true`, redirect `https://console.anthropic.com/oauth/code/callback`) and the
  existing manual-callback route. Flow orchestration (PKCE pair, state token, durable
  `oauth_flow_states` rows, device-slot single-claim, callback server, reconciliation)
  is reused unchanged.
- **Account identity re-mapping** — `Account` columns lose ChatGPT-only identity
  (`chatgpt_account_id`, `chatgpt_user_id`, `workspace_*`, `codex_installation_id`) in
  favor of Anthropic identity: `pool_class` (`oauth_seat` | `api_key`),
  `anthropic_organization_id` (from token/profile), `email`/`alias` retained, plan
  derived from subscription evidence (Pro/Max/Free) or Console tier for keys. Access +
  refresh token columns retained; `id_token` dropped (Anthropic flow does not issue one —
  profile fetched from the API instead).
- **API-key accounts** — `AccountsRepository` gains an import path for `sk-ant-*` keys:
  static credentials (no refresh), optional display alias, per-key rate-limit class.
- **Token refresh** — `app/core/auth/refresh.py` re-pointed at the Anthropic token
  endpoint (`grant_type=refresh_token` + `client_id`), refresh claims/lock machinery
  reused; long-lived tokens that omit `refresh_token` handled as `reauth_required` at
  expiry.
- **Capacity tables** — `PLAN_CAPACITY_CREDITS_*` replaced by Anthropic window semantics
  (5-hour rolling + weekly caps for subscription seats; header-reported limits for API
  keys). Final window constants land in increment 3 with live-traffic evidence; this
  change only fixes the plan-type vocabulary.

## Capabilities

### New Capabilities
- `anthropic-accounts` — spec delta in this change (see `specs/anthropic-accounts/spec.md`).

### Affected Capabilities
- `account-identity`, `account-routing` — renamed/re-mapped identity fields; eligibility
  semantics unchanged.

## Impact

- Alembic: one new revision on current head
  (`20260918_000000_merge_scim_and_overflow_heads`), single-head path, data migration
  mapping existing rows to `pool_class` + dropping ChatGPT columns (upgrade-only fork;
  no production data exists yet — downgrade is structural reverse).
- `app/modules/oauth/` UI strings and dashboard account cards show Anthropic identity.
- **Known constraint (accepted risk, documented in design.md):** Anthropic restricts
  consumer-plan OAuth to Claude Code/Claude.ai with server-side enforcement since
  2026-01; the OAuth pool presents as Claude Code clients, mirroring how claude-lb
  presents as Codex CLI. Accounts may be revoked; API-key pool is the compliant
  fallback and must be first-class, not an afterthought.
- Out of scope: proxy request path (`/v1/messages`), usage fetch endpoints, dashboard
  usage views (increments 3–4).
