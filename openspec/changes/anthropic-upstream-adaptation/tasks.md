# Tasks: anthropic-upstream-adaptation

## 1. Fork scaffold

- [x] 1.1 Clone codex-lb → claude-lb, rename remote to `upstream`
- [x] 1.2 Rebrand `pyproject.toml` (name, description, keywords, entrypoints `claude-lb` / `claude-lb-db`)
- [x] 1.3 Update CLI user-facing strings (`app/cli.py`, `app/db/migrate.py`, `app/admin_cli.py`) and Makefile `migration-check` targets
- [x] 1.4 Delete `crates/`, `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml`, `deny.toml`; drop rust targets from Makefile (`lint` ratchets, `ci-fast`, `ci` unaffected except rust removal)
- [x] 1.5 README stub with upstream attribution; remove `README.zh-CN.md`
- [x] 1.6 Record baseline: `uv sync`, ruff, focused unit slice
- [x] 1.7 Remove Rust-parity fixture tests (`tests/unit/test_native_*_fixtures.py`, read deleted `crates/` fixtures); skip POSIX-only modules (`uvloop`, `fcntl`) in `tests/conftest.py` on Windows

## 2. Anthropic account model

- [ ] 2.1 Research + document claude.ai OAuth (PKCE) flow and token refresh endpoints; document API-key account differences
- [ ] 2.2 Alembic revision(s): account type discriminator, credential fields, plan/seat metadata; single-head topology checked with `scripts/check_migration_topology.py`
- [ ] 2.3 Replace `app/modules/oauth` ChatGPT flow with Anthropic OAuth; add API-key account import path
- [ ] 2.4 Adapt account settings/validation (`app/core/config`, settings tiers) for new credential fields
- [ ] 2.5 Tests: OAuth round-trip, refresh failure handling, API-key import, migration up/down

## 3. Proxy core rewrite

- [ ] 3.1 `/v1/messages` route: validation, request-id handling, streaming (SSE) and non-streaming
- [ ] 3.2 Wire existing balancer/eligibility to Anthropic account pool; keep file-pinned/ownership invariants
- [ ] 3.3 Rate-limit windows: 5-hour + weekly caps for subscription seats; `anthropic-ratelimit-*` headers for API keys
- [ ] 3.4 Failover/retry: account exclusion on the documented degradation path only; reservations settle before error-health writes
- [ ] 3.5 Remove Codex proxy core + OpenAI-compat layer + dormant `app/core/clients/native_egress.py` once parity reached
- [ ] 3.6 Tests at the route layer: streaming, partial-failure fan-out, failover, compat paths

## 4. Usage + dashboard

- [ ] 4.1 Map Anthropic usage fields onto usage rollups; backfill strategy for schema changes
- [ ] 4.2 Model registry swap to Anthropic catalog
- [ ] 4.3 Dashboard rebrand + account-type-aware UI; before/after screenshots
- [ ] 4.4 Docs sweep: `docs/**` re-scoped to claude-lb, each page linked to owning spec
- [ ] 4.5 `.github/` workflows + `flake.nix` de-Rust + rename (required before first remote push)
- [ ] 4.6 Decide `CODEX_LB_*` env prefix: rename vs alias (compatibility decision, explicit change)
