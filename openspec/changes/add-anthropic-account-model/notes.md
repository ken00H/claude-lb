# Notes: add-anthropic-account-model

## 2026-10-04 — increment-2 close-out session

Scope: finish everything that does not require a live Claude account. Decisions
and findings recorded here so the next session inherits them.

### Landed this session (branch `chore/close-out-anthropic-account-model`)

- **Repository persistence fix**: `rotate_tokens` now accepts and persists
  `token_expires_at` / `anthropic_account_id` / `anthropic_organization_id`
  (write-only-when-present, CAS-protected like the rest of the method), and
  `_apply_account_updates` copies the anthropic fields + `pool_class` on
  reauthorized-replacement merges. Before this, `token_expires_at` was written
  once at account creation and went stale forever after the first refresh.
- **Naive-UTC expiry bug (found by the new test)**: `_perform_refresh` computed
  `int(utcnow().timestamp()) + expires_in`, but `utcnow()` returns a naive UTC
  datetime whose `.timestamp()` is interpreted as LOCAL time — on a UTC+3 host
  the stored expiry landed 2h in the past. Fixed to
  `naive_utc_to_epoch(utcnow()) + expires_in`, matching `oauth/service.py`.
- **AuthGuardian sweep exclusion**: `_auth_guardian_account_is_stale_eligible`
  returns False for `pool_class == "api_key"` so static keys are never even
  selected (previously the sweep enumerated them and no-op'd per pass).
- **`TokenRefreshResult.organization_id`**: added and populated from
  `payload.organization_uuid` so refreshes can persist the org identity.
- **Test repair**: `tests/integration/test_oauth_flow.py` was dead at
  collection (imported the removed `DeviceCode`). Device-flow-vehicle tests
  were deleted or rewritten onto the browser + manual-callback vehicle;
  ChatGPT workspace/seat-mismatch tests were replaced with Anthropic
  seat-uuid equivalents; fake `OAuthTokens` now use the Anthropic response
  shape (account/organization uuids, expires_in, email — no id_token JWT).

### Deliberate deferrals (with owners/sequencing)

- **Live verification (1.1/1.2)**: parked until a Claude account is available.
  Token endpoint path (`/v1/oauth/token` vs `/api/oauth/token`) remains
  unresolved and gates the schema-freeze sign-off.
- **"Unknown plan is routable" spec-delta alignment**: deferred by user
  decision (2026-10-04). Current pinned semantics (see
  `tests/unit/test_proxy_account_eligibility.py::test_unknown_plan_is_routable_only_without_catalog_plan_restrictions`):
  an unknown-plan seat routes when the model has no catalog entry, is excluded
  when the catalog lists plans not literally including `unknown`. The
  bootstrap model catalog is still OpenAI vocabulary; full alignment should
  ride with the Anthropic model-registry swap (umbrella 4.2) rather than
  special-casing `unknown` against a catalog that is about to be replaced.
- **`OpenAIAuthClaims` removal + profile fetch**: kept as fallback beneath the
  Anthropic mapping until the proxy-core rewrite (umbrella 3.5) stops reading
  legacy claims; profile fetch needs live endpoints (parked with 1.1).
- **Frontend slice**: dashboard manual-callback copy promotion (3.5) and the
  "Add API key" UI path (4.3) are split out as explicit frontend tasks with
  P5 screenshot requirements.
- **Legacy column drops**: intentionally additive migration; drops deferred to
  umbrella 3.5 (readers still exist across proxy/usage modules).

### CHANGELOG

Left untouched per repo rule (release-please owns `CHANGELOG.md`; its config
still points at claude-lb paths — see umbrella 4.5). Fork change notes live in
this file instead.

### Inherited red gates (pre-date this session, verified by stash-compare)

- `make typecheck` had 14 `ty` diagnostics at baseline (nullable
  `id_token_encrypted` fallout from 57a511c6 + the then-dead
  `test_oauth_flow.py`); this session cleared all of them.
- `make test-unit` has 11 pre-existing failures at baseline (identical set
  with this branch's changes stashed), all fork-migration debt in files this
  change never touched: 4× `test_native_egress_packaging.py` (reference the
  Rust `crates/` deleted in the rebrand), 2×
  `test_proxy_header_launcher_contract.py` (expect `uv run claude-lb` in the
  READMEs the fork stubbed/deleted), 1× `test_docker_networking.py`,
  4× `test_db_migrate.py` (legacy revision-remap tests). Fixing these is
  rebrand-cleanup scope (umbrella 4.5 family), not account-model scope.
- The `openspec` CLI (`@fission-ai/openspec` via npx) is not resolvable from
  this environment, so local `openspec validate --specs` was unavailable;
  CI runs it on GitHub. This session's OpenSpec edits are tasks/notes only
  (no spec deltas touched).
