# Context: anthropic-upstream-adaptation

## Decisions

- **Fork, not extend**: claude-lb is a separate product from claude-lb. Extending claude-lb
  with a second provider would drag Anthropic semantics through Codex-specific routing,
  protocol, and spec layers. The fork keeps the generic machinery (DB, balancer,
  eligibility, dashboard, settings tiers, migrations) and replaces the ecosystem layer.
- **Both pool classes**: claude.ai OAuth seats are primary; Anthropic API keys are a
  first-class second pool class, not an afterthought. They differ in rate-limit semantics
  (usage windows vs. header-reported limits) and credential lifecycle (refresh tokens vs.
  static keys) — the account model must make that distinction explicit, not bolt it on.
- **Anthropic-only downstream**: clients speak `/v1/messages`. No OpenAI-compat
  translation layer in v1; claude-lb's compat layer exists because Codex upstream is not
  OpenAI-shaped, but Anthropic upstream already is Anthropic-shaped.
- **No silent renames of operator-facing contracts**: `CLAUDE_LB_*` env prefix, DB URLs,
  and existing dashboard routes stay until each is explicitly re-decided. Renames are
  compatibility changes and get their own spec deltas.

## Constraints inherited from claude-lb review practice

These apply to every increment (see claude-lb AGENTS.md "PR Readiness / Review Trapdoors"):

- File-pinned requests must not cross accounts; API-key/resource reservations must settle
  before error-health writes; excluded accounts must actually leave the selection loop.
- Do not share one `AsyncSession` across concurrent tasks; cancel/await spawned tasks on
  failure; test partial-failure paths, not just all-success.
- Migrations: single-head Alembic topology on current `main` parent, up/down coverage,
  backfills for new fields over existing rows.

## Increment 1 baseline (recorded 2026-10-01, Windows dev box)

- `uv run ruff check .` + `ruff format --check`: clean. `uv run ty check`: clean.
- `uv run pytest tests/unit -q`: **10089 passed, 155 failed, 103 skipped** (24 min).
- Failure census: 69 in `test_native_egress.py` + 4 in `test_native_egress_packaging.py`
  (test the optional Rust binary machinery slated for removal in increment 3), ~12 in
  `test_traffic_*` / `test_check_*_safety` / `test_check_proxy_architecture` (POSIX
  subprocess and path-separator assumptions), ~15 in `test_codex_body_*` /
  `test_passthrough_request_fields` (same family), plus singles in git/deploy tooling
  tests (`test_changed_openspec_ci`, `test_docker_networking`, `test_helm_replica_artifacts`,
  `test_cleanup_superseded_beta_prs`).
- Sampled verdict: failures are Windows-environment artifacts (hardcoded POSIX path
  separators, POSIX-only subprocess plumbing), not rebrand breakage — upstream CI is
  Linux-first. Canonical gate for claude-lb is Linux CI; Windows numbers are a local
  reference only. Do not "fix" these test files for Windows — they get replaced or
  deleted by increments 2–4 alongside the machinery they cover.

## Ecosystem notes (verify at implementation time — researched, not confirmed)

- Claude subscription limits are usage-based (roughly 5-hour rolling windows plus weekly
  caps), reported via response headers rather than a documented quota API; the header
  names/shapes must be confirmed against live traffic in increment 3.
- Claude Code OAuth uses a public PKCE client; token refresh and revocation endpoints and
  the required scopes must be confirmed in increment 2 before schema work is finalized.
- API-key accounts expose `anthropic-ratelimit-*` and `anthropic-priority-*` style
  response headers; per-key ILM limits differ by tier.

## Failure modes to avoid

- Keeping Codex-era account-eligibility heuristics (plan detection, force probes) that
  have no Anthropic equivalent — replace, do not translate.
- Streaming: SSE passthrough must preserve event ordering and terminations; buffered
  re-emission is a compatibility break for Claude Code clients.
- Dashboard assumptions about "5-hour prompt windows" from claude-lb are coincidence, not
  parity — re-derive every window calculation from Anthropic semantics.
