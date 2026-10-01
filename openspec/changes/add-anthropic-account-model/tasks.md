# Tasks: add-anthropic-account-model

## 1. Live verification (before schema freeze)

- [ ] 1.1 Capture one real OAuth token exchange + one refresh from a Claude Code login (user-supplied capture or fresh flow); record the true token endpoint path, response fields (org/account uuids, scope, expires_in), and whether a plan indicator appears anywhere
- [ ] 1.2 Record Pro/Max subscription limit evidence (response headers on a real `/v1/messages` call with an OAuth token) for increment 3's window constants

## 2. Schema

- [ ] 2.1 Alembic revision on `20260918_000000_merge_scim_and_overflow_heads`: add `pool_class`, `anthropic_organization_id`, `anthropic_account_id`; backfill existing rows to `pool_class='oauth_seat'`; drop `chatgpt_*`, `workspace_*`, `codex_installation_id`, `id_token_encrypted`; rename plan vocabulary to `pro|max|free|console|unknown`
- [ ] 2.2 Update `app/db/models.py`, repositories, and `make lint` topology check passes (single head)

## 3. OAuth client swap

- [ ] 3.1 `app/core/config/settings.py`: replace `AUTH_BASE_URL`/`OAUTH_*` constants with Anthropic values (token path behind one constant pending 1.1)
- [ ] 3.2 `app/core/clients/oauth.py`: authorize URL + params (`code=true`, copy/paste redirect), token exchange JSON body (no client secret), PKCE S256 reuse, `CODE#STATE` split on manual callback
- [ ] 3.3 `app/core/auth/`: replace `OpenAIAuthClaims` with Anthropic token/profile mapping; `refresh.py` → Anthropic refresh grant; keep previous refresh token when response omits one
- [ ] 3.4 `app/modules/oauth/service.py`: `_persist_tokens` maps Anthropic identity; manual-callback promoted to primary path in dashboard UI copy; profile fetch fills email/organization when reachable

## 4. API-key accounts

- [ ] 4.1 `AccountsRepository.import_api_key`: validate `sk-ant-` prefix, encrypt as access credential, `pool_class='api_key'`, plan `console|unknown`
- [ ] 4.2 Exclude `api_key` rows from token-refresh sweeps; dashboard accounts page gains an "Add API key" path

## 5. Tests + docs

- [ ] 5.1 Route-layer tests: OAuth start/manual-callback/complete round trip with mocked Anthropic endpoints; refresh contention; missing-refresh-token handling
- [ ] 5.2 Migration up/down test; eligibility tests for `unknown` plan routability
- [ ] 5.3 Update `docs/` account pages + `openspec validate --specs`
