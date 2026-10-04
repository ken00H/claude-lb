# Tasks: add-anthropic-account-model

## 1. Live verification (before schema freeze)

- [ ] 1.1 Capture one real OAuth token exchange + one refresh from a Claude Code login (user-supplied capture or fresh flow); record the true token endpoint path, response fields (org/account uuids, scope, expires_in), and whether a plan indicator appears anywhere — *parked 2026-10-04: no Claude account available yet*
- [ ] 1.2 Record Pro/Max subscription limit evidence (response headers on a real `/v1/messages` call with an OAuth token) for increment 3's window constants — *parked with 1.1*

## 2. Schema

- [x] 2.1 Alembic revision on `20260918_000000_merge_scim_and_overflow_heads`: add `pool_class`, `anthropic_organization_id`, `anthropic_account_id`; backfill existing rows to `pool_class='oauth_seat'`; rename plan vocabulary to `pro|max|free|console|unknown` *(landed additively in 57a511c6 + `token_expires_at` bonus; the `chatgpt_*`/`workspace_*`/`codex_installation_id` column drops are deliberately deferred until the proxy core rewrite stops reading them — umbrella task 3.5; `unknown` is the default plan rather than a catalog member, and full vocabulary narrowing rides with the model-registry swap in umbrella 4.2)*
- [x] 2.2 Update `app/db/models.py`, repositories, and `make lint` topology check passes (single head) *(repositories now persist the anthropic identity + `token_expires_at` through `rotate_tokens`/`_apply_account_updates`; completed 2026-10-04)*

## 3. OAuth client swap

- [x] 3.1 `app/core/config/settings.py`: replace `AUTH_BASE_URL`/`OAUTH_*` constants with Anthropic values (token path behind one constant pending 1.1)
- [x] 3.2 `app/core/clients/oauth.py`: authorize URL + params (`code=true`, copy/paste redirect), token exchange JSON body (no client secret), PKCE S256 reuse, `CODE#STATE` split on manual callback
- [x] 3.3 `app/core/auth/`: replace `OpenAIAuthClaims` with Anthropic token/profile mapping; `refresh.py` → Anthropic refresh grant; keep previous refresh token when response omits one *(Anthropic payload mapping landed and is primary; `OpenAIAuthClaims` retained as a claim-parsing fallback beneath it — full removal rides with 3.6 below and umbrella 3.5)*
- [x] 3.4 `app/modules/oauth/service.py`: `_persist_tokens` maps Anthropic identity; manual-callback promoted to primary path in dashboard UI copy; profile fetch fills email/organization when reachable *(identity mapping landed; the backend manual-callback IS the primary exchange path — the dashboard copy promotion and profile fetch are split out below)*
- [ ] 3.5 Promote the manual-callback copy in the dashboard OAuth dialog (frontend slice; before/after screenshots per PRINCIPLES P5)
- [ ] 3.6 Profile/organization fetch to fill email + plan evidence *(needs live endpoints; parked with 1.1)*

## 4. API-key accounts

- [x] 4.1 `AccountsRepository.import_api_key`: validate `sk-ant-` prefix, encrypt as access credential, `pool_class='api_key'`, plan `console|unknown` *(landed as `AccountsService.import_api_key` + `POST /api/accounts/import-api-key`; plan is always `console`)*
- [x] 4.2 Exclude `api_key` rows from token-refresh sweeps *(two layers: `AuthManager.refresh_account` no-ops on the pool class, and the Auth Guardian candidate sweep pre-filters it out; completed 2026-10-04)*
- [ ] 4.3 Dashboard accounts page gains an "Add API key" path (frontend slice; before/after screenshots per PRINCIPLES P5)

## 5. Tests + docs

- [x] 5.1 Route-layer tests: OAuth start/manual-callback/complete round trip with mocked Anthropic endpoints; refresh contention; missing-refresh-token handling *(repaired `test_oauth_flow.py` — device-flow vehicle tests replaced with browser/manual-callback equivalents; added refresh-retention, anthropic-persistence, api_key no-op, and seat-mismatch tests in `test_auth_manager.py`/`test_oauth_flow.py`)*
- [x] 5.2 Migration up/down test; eligibility tests for `unknown` plan routability *(migration round-trip for `20261001_000000`; unknown-plan eligibility pinned to current semantics — routable without catalog restrictions, excluded by catalog-listed plans; spec-delta alignment deferred, see notes.md)*
- [x] 5.3 Update `docs/` account pages + `openspec validate --specs` *(account-flow pages re-scoped to the Anthropic flows, linking to this change's delta spec; the full docs sweep remains umbrella task 4.4)*
