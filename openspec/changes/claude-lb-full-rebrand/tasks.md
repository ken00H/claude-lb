# Tasks: claude-lb-full-rebrand

## 1. Runtime identity

- [ ] 1.1 `settings.py`: `env_prefix="CLAUDE_LB_"`, `~/.claude-lb`, `/var/lib/claude-lb`
- [ ] 1.2 Repo-wide `CODEX_LB_` → `CLAUDE_LB_` (app, tests, docs, scripts, deploy, .env.example, Makefile, .github, AGENTS.md)
- [ ] 1.3 Prometheus `codex_lb_*` → `claude_lb_*` incl. bundled Grafana dashboards
- [ ] 1.4 Migration advisory-lock key + migrate.py attrs key → `claude_lb`
- [ ] 1.5 Helm chart dir/name/values, docker-compose, Makefile, Dockerfile paths, release-please config, `uv lock` regen

## 2. Dashboard surfaces

- [ ] 2.1 i18n locales (en/ko/zh-CN): product mentions, env-var copy, Claude upstream wording
- [ ] 2.2 localStorage keys `claude-lb-*`, OIDC postMessage channels `claude-lb*`
- [ ] 2.3 Brand component rename (`codex-logo.tsx` → `claude-logo.tsx`)

## 3. API + DB

- [ ] 3.1 Alembic revision renaming `apply_to_codex_model` → `apply_to_default_model` (up/down, single parent)
- [ ] 3.2 ORM model, api_keys schemas/service, frontend schemas + tests + mocks
- [ ] 3.3 Telemetry `app_name`, runtime release URL, fleet source → claude-lb

## 4. Specs + docs

- [ ] 4.1 Delta specs for configuration-tiers, deployment-installation, telemetry
- [ ] 4.2 Uniform textual rename across openspec/specs and docs for renamed tokens

## 5. Verification

- [ ] 5.1 `make lint`, `make typecheck`, `make test-unit`, frontend gates, `make migration-check`
- [ ] 5.2 Server smoke on :2477 from `~/.claude-lb` (dashboard loads, no accounts, env prefix live)
- [ ] 5.3 Archive change after live `/v1/messages` verification unblocks the adaptation roadmap
