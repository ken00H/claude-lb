# Design: claude-lb-full-rebrand

## Context

The fork already renamed the package (`pyproject.toml`, CLI entry points) but the
runtime identity — env prefix, data dir, metrics, deploy artifacts — still says
codex-lb. Increment 1 of the adaptation roadmap deliberately deferred the env
prefix question (`CODEX_LB_*` rename-vs-alias, umbrella item 4.6). This change
resolves it: rename outright, no alias.

## Decisions

### D1: Hard break, no alias for `CODEX_LB_*`

An alias (`CLAUDE_LB_X or CODEX_LB_X`) would violate the no-speculative-fallback
convention (one canonical name per setting) and permanently double the
configuration surface. The project has exactly one operator (the fork owner);
all affected deployments are under their control. Cost: every deployment must
rename env vars once. Accepted.

### D2: Data directory moves by directory rename, not by code migration

`mv ~/.codex-lb ~/.claude-lb` preserves the SQLite store, encryption key, and
usage history. In-app auto-migration (detecting the old dir) would be a
speculative fallback for a one-time event and is not built.

### D3: Prometheus metric names rename (`codex_lb_*` → `claude_lb_*`)

Metric names are the product identity shown in dashboards and alerts. Bundled
Grafana dashboards (helm values) are updated in the same commit so the bundled
surface stays self-consistent. External scrapers of the old names break;
accepted under the same rationale as D1.

### D4: Product identity vs protocol facts

Strings and identifiers naming *our product* are renamed. Names that *describe
the Codex ecosystem* (auth.json export format, ChatGPT OAuth mode of legacy
seats, Codex CLI session tooling, the inert Codex proxy core slated for removal
in increment 3.5/4.5) keep their names — renaming them would make the code and
copy lie about what they do. See proposal "Deliberately NOT renamed".

### D5: `apply_to_codex_model` → `apply_to_default_model`

The field's semantic is "rewrite the model field to the pool's default model";
`codex` was upstream branding, not semantics. Renamed through the full stack
(DB column via new forward-only Alembic revision with up/down, ORM model, API
schemas, service, frontend schemas, tests).

### D6: Spec propagation

Key normative requirements that name the renamed surfaces carry explicit deltas
(`configuration-tiers`, `deployment-installation`, `telemetry`). The remaining
mentions across `openspec/specs/` and `docs/` are uniform textual renames of the
same tokens; they are applied in the same commits so specs never promise the old
names. `openspec validate --specs` is run in CI (the CLI is not resolvable on
this machine — known baseline limitation).

## Risks / Trade-offs

- Any environment/scraper/script outside the operator's control that references
  `CODEX_LB_*`, `~/.codex-lb`, or `codex_lb_*` metrics breaks silently. Mitigation:
  the change notes list the three tokens to grep for.
- The rename sweep is large but mechanical; the architecture ratchets
  (`make lint`), settings-tier checker, and the 521-file test suite bound the
  blast radius.
