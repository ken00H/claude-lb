# Proposal: claude-lb-full-rebrand

## Why

claude-lb is a fork of Soju06/codex-lb whose surface still carries the upstream
product identity everywhere it matters operationally:

- environment prefix `CODEX_LB_*` (~250 distinct variable names)
- default data directory `~/.codex-lb` (and `/var/lib/codex-lb` in containers)
- Prometheus metric family `codex_lb_*` (65 distinct names)
- deploy identity: Helm chart `deploy/helm/codex-lb/`, image names, docker-compose
  resources, Makefile targets, release-please wiring
- dashboard copy, i18n locales, localStorage keys, and OIDC postMessage channels
- API contract fields naming the product (`apply_to_codex_model`, telemetry
  `app_name: "codex-lb"`, runtime release URL, fleet source string)

The package itself (`pyproject.toml`) and the CLI entry points are already
`claude-lb`; the runtime identity lags behind. This change completes the rebrand
so operators, scrapers, and users see one product name.

## What Changes

- **Environment prefix**: `CODEX_LB_` → `CLAUDE_LB_` (single source:
  `app/core/config/settings.py` `env_prefix`, plus every documentation, deploy,
  and test reference). **Hard break — no alias.** Rationale in design.md.
- **Data directory**: `~/.codex-lb` → `~/.claude-lb`; container dir
  `/var/lib/codex-lb` → `/var/lib/claude-lb`. Existing installs migrate by moving
  the directory; no in-app migration path.
- **Metrics**: `codex_lb_*` → `claude_lb_*` (breaking for external scrapers;
  bundled Grafana dashboards updated in the same change).
- **Deploy identity**: chart directory and name `claude-lb`, image repository
  `ken00H/claude-lb`, docker-compose resources, Makefile targets,
  release-please paths.
- **Product-identity API fields**: `apply_to_codex_model` →
  `apply_to_default_model` (API field, DB column, frontend schema; new Alembic
  revision), telemetry `app_name` → `"claude-lb"`, runtime release URL →
  `ken00H/claude-lb`, fleet source → `"claude-lb fleet observability"`.
- **Dashboard surfaces**: i18n copy, localStorage key prefix `claude-lb-`,
  OIDC postMessage channel prefix `claude-lb`, brand component rename.
- **Specs/docs**: uniform textual rename across `openspec/specs/` and `docs/`
  for every renamed surface; key normative requirements carry explicit deltas
  in this change.

## Deliberately NOT renamed

Names that are facts about the Codex protocol or upstream product, not our
brand, keep their names until the roadmap reworks those capabilities
(increment 3.5 / 4.2):

- `CodexAuthJson` / `codex_auth_json` (exports the Codex CLI auth.json format)
- `auth_mode: "chatgpt"` (describes the upstream OAuth mode of legacy seats)
- `chatgpt_account_id`, `codex_installation_id` (hold ChatGPT/Codex identifiers)
- `CodexModelsResponse`, `CodexTruncationPolicy` and the inert Codex proxy core
  (`app/core/clients/codex*.py`) — slated for removal/adaptation, renaming dead
  code is churn
- `codex-sessions retag` CLI subcommand (operates on Codex CLI session files)

## Impact

- **Breaking** (capability: configuration-tiers, deployment-installation,
  telemetry): every deployment must rename env vars, move the data directory,
  and update metric scrapers. No compatibility alias is provided (sole-operator
  project; alias would double the configuration surface forever).
- **Breaking** (capability: api-keys): `apply_to_codex_model` renamed in the
  API payload/response and DB schema.
- Affected specs: `configuration-tiers`, `deployment-installation`, `telemetry`
  (deltas here); the uniform rename also touches prose in `runtime-portability`,
  `database-migrations`, `release-management`, and others (handled as textual
  sync, see tasks).
