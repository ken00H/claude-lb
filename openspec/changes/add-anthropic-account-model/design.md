# Design: add-anthropic-account-model

## Source facts (graded)

Confirmed by two independent secondary sources (Claude Code SDK OAuth gist; Claude
OAuth API write-up), all to be re-verified against live traffic during implementation:

| Fact | Value | Grade |
|---|---|---|
| Authorize endpoint | `https://claude.ai/oauth/authorize` | two sources agree; probed 2026-10-01: alive (302) but geo-blocked from dev machine (`app-unavailable-in-region`) |
| Token endpoint host | `console.anthropic.com` now **301s to `platform.claude.com`** (probed 2026-10-01) — both source docs stale; canonical host is `platform.claude.com` | probed live |
| Token endpoint path | `/v1/oauth/token` vs `/api/oauth/token` | **unresolved — probes blocked at edge (403 envelope before OAuth logic); verify with real login** |
| Client ID | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` (Claude Code CLI's; may rotate) | two sources agree |
| PKCE | S256, base64url SHA-256 of verifier | confirmed |
| Callback | copy/paste mode (`code=true`, redirect `https://console.anthropic.com/oauth/code/callback`); pasted value may be `CODE#STATE` | confirmed |
| Scopes | `user:inference` (+ `user:profile`, `org:create_api_key` optional) | confirmed |
| Access token | `sk-ant-oat01-…`, Bearer, ~8 h expiry | confirmed |
| Refresh token | `sk-ant-ort01-…`; long-lived variant may omit it | confirmed |
| Token response | JSON, includes organization + account `uuid` fields; no `id_token` | confirmed |
| Plan type | **no plan claim seen in token** — derive via profile/organization API or first-traffic observation | open — verify live |
| Consumer OAuth restriction | Anthropic blocks consumer-plan OAuth outside Claude Code/Claude.ai (server-side since 2026-01, Feb 2026 ToS); third-party clients (Goose, OpenCode) dropped OAuth | reported by Moltis docs citing Anthropic policy — treat as true until live-verified |

## Probe notes (2026-10-01)

- Token-endpoint probes from the dev machine return Anthropic's structured
  `{"error":{"type":"forbidden","message":"Request not allowed"}}` envelope on every
  candidate path — edge-level blocking, not an OAuth verdict. Path discrimination
  requires a real login (task 1.1).
- `claude.ai` is region-blocked from the dev machine. The OAuth flow (authorize visit
  and token exchange) must run through the proxy-pool machinery (claude-lb already routes
  token exchanges through `ResolvedUpstreamRoute`); the operator's browser session may
  also need egress from a supported region.

## Decisions

1. **Reuse the claude-lb OAuth orchestration wholesale.** `OauthService`'s durable flow
   store, reconciliation, device-slot claiming, manual-callback route, and callback
   server are provider-agnostic; only the low-level client constants and claim mapping
   change. Manual-callback becomes the *primary* flow (Anthropic's copy/paste callback
   matches it exactly); the localhost listener is kept for parity but is not required.
2. **`pool_class` discriminator on `accounts`.** `oauth_seat` (subscription, refreshable,
   window-based limits) vs `api_key` (static, header-reported limits). Balancer treats
   both as accounts; increment 3 wires per-class rate-limit inputs. One table, one
   eligibility path — no second account table.
3. **Identity without id_token.** Anthropic issues no id_token; identity comes from
   token-response `uuid` fields plus a profile fetch. `Account.email` stays nullable
   until profile resolves it. Drop `chatgpt_*`, `workspace_*`, `codex_installation_id`;
   add `anthropic_organization_id`, `anthropic_account_id` (both from token/profile).
4. **Plan vocabulary now, constants later.** This change fixes plan types to
   `pro | max | free | console | unknown`. Window capacities (5 h/weekly) land in
   increment 3 with live evidence — no invented numbers.
5. **API-key import is a first-class dashboard flow**, not a seed script: same accounts
   table, validation of `sk-ant-…` prefix, no refresh scheduler for `api_key` rows
   (skip in refresh sweeps).
6. **Claude Code impersonation is deferred to increment 3** (request-path headers) but
   recorded here because it bounds account-model choices: credentials are stored so that
   any per-request headers derived from them (client id, beta headers) can be added
   without schema churn.

## Accepted risk (explicit, user-visible)

Pooling consumer OAuth seats through a non-Claude-Code client violates Anthropic's
February 2026 terms, with server-side enforcement reported since January 2026. The
mitigation is the same one claude-lb uses against OpenAI's equivalent constraint:
present upstream traffic as the official client. Consequences to accept: token
revocation or account bans are possible at any time; the API-key pool is the compliant
fallback and must remain fully functional on its own.

## Failure modes to design against

- Refresh endpoint disagreement (`/v1/` vs `/api/`) — implement via one constant, verify
  with a live refresh before merging increment 2.
- Refresh response missing `refresh_token` (rotation or long-lived variant) — keep the
  previous refresh token if the response omits one, and mark rows without any as
  `reauth_required` at expiry rather than crash-looping the sweeper.
- Plan detection ambiguity — default `unknown` must be routable (eligible), not blocked,
  so a token whose plan cannot be read still serves traffic.
