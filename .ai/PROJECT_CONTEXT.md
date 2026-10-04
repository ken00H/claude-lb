<!-- AI ENGINEERING PACK: PROJECT-OWNED FILE -->
# Project Context

## Product

- **Name:** claude-lb
- **Purpose:** A Claude/Anthropic account load balancer and proxy: one Anthropic-compatible `/v1/messages` endpoint in front of a pool of Claude accounts (claude.ai OAuth seats + Anthropic API keys), with rate-limit tracking, failover, and a usage dashboard.
- **Primary users:** Developers/teams pointing Claude or Anthropic API clients at a single local endpoint backed by multiple accounts.
- **Current maturity:** Work-in-progress fork. Rebranded from `Soju06/codex-lb`; of the 4 increments in `openspec/changes/anthropic-upstream-adaptation/`, only 1 (rebrand) and 2 (Anthropic account model) have landed. The bundled Codex proxy core is inert reference code until increment 3 (Anthropic proxy-core rewrite) lands.
- **Repository owner:** Fork maintainer (upstream: Soju06).
- **Last reviewed:** 2026-10-03, AI coding agent (ZCode).

## Technology

- **Languages and versions:** Python 3.13 (uv-managed), TypeScript (React 19), SQL (Alembic).
- **Frameworks:** FastAPI (+ uvicorn), SQLAlchemy 2 async, Pydantic v2 / pydantic-settings, aiohttp (egress); frontend React 19 + Vite + Tailwind v4.
- **Runtime and package tools:** `uv` (Python, `uv.lock`), `bun` (frontend, `frontend/bun.lock`), `make` (task entry points), `ty` for typechecking (not mypy).
- **Primary data stores:** SQLite (aiosqlite, default; data dir `~/.codex-lb`) and PostgreSQL (asyncpg).
- **External services:** Anthropic/Claude upstream (claude.ai OAuth + api.anthropic.com). Optional extras: Prometheus metrics, OpenTelemetry tracing.
- **Supported platforms:** Linux/macOS via uv or Docker; Kubernetes via `deploy/helm/codex-lb/`.

## Architecture

- **System shape:** Modular monolith: FastAPI app + proxy serving a built SPA from `app/static/`.
- **Entry points:** `app/cli.py` (`claude-lb` serve CLI), `app/main.py` (FastAPI app factory), `app/db/migrate.py` (`claude-lb-db`), `app/admin_cli.py`.
- **Business rules:** `app/modules/<feature>/service.py` and `app/core/balancer/`.
- **Public contracts:** `app/modules/*/api.py` — proxy routes (`/v1/messages`), dashboard API (`/backend-api`), SCIM v2, websocket bridges.
- **Persistence:** `app/db/models.py`; Alembic revisions in `app/db/alembic/versions/`.
- **Frontend features:** `frontend/src/` (SPA; builds into `app/static/`).
- **Configuration:** `app/core/config/settings.py` + `app/core/config/tiers.py`; precedence code < env (`CODEX_LB_*`) < dashboard override.
- **Generated files:** `app/static/**` → built from `frontend/` by `bun run build`; `CHANGELOG.md` → release-please (never hand-edit).
- **Architecture decisions:** `openspec/specs/**` (SSOT); `spec/` holds the TLA+ ownership model (`CoreOwnership.tla`).

## Non-negotiable invariants

- File-pinned requests must not cross accounts; API-key reservations must settle before error-health writes.
- Excluded accounts actually leave the selection loop; idle disconnects must not mark otherwise healthy accounts unhealthy.
- Never share one `AsyncSession` across concurrent tasks; cancel or await spawned tasks on failure; test partial-failure paths, not only all-success.
- Alembic graph stays single-head with a single upgrade path; run `make lint` (includes `scripts/check_migration_topology.py`) after adding any revision.
- Never log credentials, tokens, or request bodies. No speculative fallbacks (e.g. `os.getenv("A") or os.getenv("B")`); fail fast.

## Local commands

| Purpose | Working directory | Command | Notes |
| --- | --- | --- | --- |
| Install | repo root | `uv sync --all-extras --dev` | Python deps; frontend: `cd frontend && bun install --frozen-lockfile` |
| Format | repo root | `uv run ruff format .` | Writes; `make lint` runs the check-only variant |
| Lint/static analysis | repo root | `make lint` | ruff check + format check + 5 `scripts/check_*.py` architecture ratchets |
| Typecheck | repo root | `make typecheck` | Astral `ty`; frontend: `make frontend-typecheck` |
| Focused tests | repo root | `uv run pytest tests/unit/<file>::<test>` | pytest asyncio_mode=auto, session loop scope |
| Full tests (unit) | repo root | `make test-unit` | Needs `make frontend-build` first (backend tests read `app/static/`) |
| Coverage | repo root | `make frontend-test` | Vitest with 70% thresholds (same as CI) |
| Integration | repo root | `make test-integration-core` / `make test-integration-bridge` | SQLite; `make test-postgres` needs local PostgreSQL |
| End-to-end | repo root | `make test-e2e`; `make test-dashboard-browser-smoke` | Browser smoke needs Playwright chromium (auto-installed by target) |
| Build | repo root | `make frontend-build`, `make package` | SPA → `app/static/`; sdist/wheel + asset verification |
| Generate | repo root | `make migration-check` | Applies migrations to a throwaway SQLite DB and runs `claude-lb-db check` |
| Security | repo root | `make docker` | Builds image and runs trivy (CRITICAL, exit 1) |

## Safe local environment

- **Disposable test data:** Tests use temp SQLite DBs (mktemp under `/tmp`, auto-removed) and fixtures under `tests/fixtures/`.
- **Allowed local services:** Loopback only — server `127.0.0.1:2455`, Vite dev `5173`, test PostgreSQL `127.0.0.1:5432` (`codex_lb`/`codex_lb` test credentials).
- **Forbidden targets:** Production, real Anthropic accounts or tokens, shared databases, any external system.
- **Secret handling:** `.env` files loaded via `CODEX_LB_ENV_FILE`; `GITHUB_TOKEN` comes from the environment. Never commit or log values.

## Quality gates

- GitHub Actions CI (14 workflows): lint, typecheck, frontend lint/type/test, unit + integration shards (`.github/scripts/pytest_shards.py`), bridge, e2e, postgres, migrations, package, docker/trivy, helm, simplicity budgets.
- Simplicity budgets enforced by `.github/scripts/check_simplicity_budgets.py` per `.github/simplicity-budgets.toml`: README ≤200 lines, `.env.example` ≤60, dashboard core-nav ≤5 items, `Settings.model_fields` ≤96, and a complete root-entry allowlist.
- CodeRabbit review: unresolved actionable threads on the current head block readiness.
- OpenSpec: behavior/API/schema/dashboard changes need a change folder under `openspec/changes/` and `openspec validate --specs` must pass.

## Production boundary

- **Deployment owner:** Fork maintainer.
- **Release process:** release-please (generates `CHANGELOG.md`); see `.github/CONTRIBUTING.md`.
- **Runbooks:** `docs/deployment/{docker,kubernetes,remote}.md`.
- **Observability:** optional `metrics`/`tracing` extras (`app/core/metrics/`, `app/core/tracing/`).
- **Migrations:** auto-applied at startup (`app/db/migrate.py`) with advisory-lock serialization; policy details in `AGENTS.md` (PR Readiness / Review Trapdoors).
- **Actions always requiring explicit approval:** commit/push/PR (per `.agents/conventions/git-workflow.md`), deploy, production migrations, releases, PR merges.

## Known limitations

- The Anthropic proxy-core rewrite (increment 3 of the adaptation roadmap) has not landed; the proxy does not yet serve `/v1/messages`.
- Upstream naming residue: env prefix `CODEX_LB_*`, Helm chart `deploy/helm/codex-lb/`, docs URLs, and `docs/rust-architecture.md` (describes `crates/` deleted in this fork — no Rust code exists).
- `make test-postgres`, `make docker`, `make helm-check`, and `make helm-smoke-kind` need local PostgreSQL/Docker/helm/kind that may not be installed.
