# AGENTS

## Environment

- Python: `>=3.13` via `uv` (`uv sync --frozen`; interpreter at `.venv/bin/python`)
- GitHub auth for git/API is available via env vars: `GITHUB_USER`, `GITHUB_TOKEN` (PAT). Do not hardcode or commit tokens.
- For authenticated git over HTTPS in automation, use: `https://x-access-token:${GITHUB_TOKEN}@github.com/<owner>/<repo>.git`

## Project Snapshot

- **What**: `claude-lb` — Claude/Anthropic account load balancer and proxy (`/v1/messages`) with a usage dashboard. FastAPI backend + React 19 SPA served from `app/static/`. Fork of `Soju06/codex-lb`, rebranded; adaptation roadmap: `openspec/changes/anthropic-upstream-adaptation/`.
- **Status**: WIP — the Anthropic proxy-core rewrite (roadmap increment 3) has not landed; the bundled Codex proxy core is inert reference code, not a working proxy.
- **Layout**: `app/core/` (reusable: balancer, settings/tiers, auth, HTTP clients, middleware) · `app/modules/` (API-facing features, each `api.py`/`service.py`/`repository.py`/`schemas.py`) · `app/db/` (models + Alembic revisions in `app/db/alembic/versions/`) · `frontend/` (React 19 + TS + Vite + Tailwind v4, **Bun**, builds into `app/static/`) · `tests/{unit,integration,e2e,simulation,load}` · `scripts/` (fitness-ratchet checkers + tooling) · `spec/` (TLA+ model) · `deploy/helm/codex-lb/` · `docs/` (mkdocs).
- `CLAUDE.md` is a symlink to `AGENTS.md`; `.claude` symlinks to `.agents`.

### Commands

| Purpose | Command |
| --- | --- |
| Deps | `uv sync --all-extras --dev`; frontend: `cd frontend && bun install --frozen-lockfile` |
| Run | `uv run claude-lb` (default `127.0.0.1:2455`); frontend dev: `cd frontend && bun run dev` |
| Lint | `make lint` (ruff check + format check + 5 `scripts/check_*.py` architecture ratchets) |
| Typecheck | `make typecheck` (Astral `ty`, not mypy) |
| Unit tests | `make test-unit` (focused: `uv run pytest tests/unit/<file>::<test>`) |
| Integration | `make test-integration-core` (or `-1/-2/-3` shards), `make test-integration-bridge`, `make test-e2e`, `make test-postgres` |
| Frontend | `make frontend-lint` / `frontend-typecheck` / `frontend-test` |
| Migrations | `make migration-check`, or `uv run claude-lb-db --db-url <url> upgrade head` then `... check` |
| Fast gate | `make ci-fast` (lint + typecheck + frontend-test + unit + package) |

### Gotchas

- Env prefix is still **`CODEX_LB_*`** (data dir defaults to `~/.codex-lb`) despite the claude-lb rebrand; `PORT` is the only exception.
- `app/static/` does not exist until `make frontend-build` runs — backend test targets depend on the built dashboard.
- New tracked root files need an entry in the `.github/simplicity-budgets.toml` root allowlist; README sections, `.env.example`, and dashboard core-nav are budgeted there too.
- `docs/rust-architecture.md` describes the `crates/` workspace this fork deleted — no Rust code exists in the tree.
- Before touching proxy/accounts read `openspec/changes/anthropic-upstream-adaptation/`; before any settings change read `openspec/specs/configuration-tiers/`; before PRs read `PRINCIPLES.md` + `.github/CONTRIBUTING.md`.

## Code Conventions

The `/project-conventions` skill is auto-activated on code edits (PreToolUse guard).

| Convention | Location | When |
|-----------|----------|------|
| Code Conventions (Full) | `/project-conventions` skill | On code edit (auto-enforced) |
| Git Workflow | `.agents/conventions/git-workflow.md` | Commit / PR |

## Workflow (OpenSpec-first)

This repo uses **OpenSpec as the primary workflow and SSOT** for change-driven development.

### How to work (default)

1) Find the relevant spec(s) in `openspec/specs/**` and treat them as source-of-truth.
2) If the work changes behavior, requirements, contracts, or schema: create an OpenSpec change in `openspec/changes/**` first (proposal -> tasks).
3) Implement the tasks; keep code + specs in sync (update `spec.md` as needed).
4) Validate specs locally: `openspec validate --specs`
5) When done: verify + archive the change (do not archive unverified changes).

### Source of Truth

- **Specs/Design/Tasks (SSOT)**: `openspec/`
  - Active changes: `openspec/changes/<change>/`
  - Main specs: `openspec/specs/<capability>/spec.md`
  - Archived changes: `openspec/changes/archive/YYYY-MM-DD-<change>/`

## Documentation & Release Notes

- **OpenSpec is the SSOT for feature/behavior documentation.** User-facing rendering lives under `docs/` (the published docs pages), and each spec-governed page MUST link back to the owning `openspec/specs/<capability>/` entry. Do not create `docs/` content that has no OpenSpec counterpart, and do not add feature docs as new README sections. Keep normative requirements in `openspec/specs/<capability>/spec.md` and free-form rationale in the capability's `context.md` (or change-level context under `openspec/changes/<change>/context.md`).
- **Do not edit `CHANGELOG.md` directly.** Leave changelog updates to the release process; record change notes in OpenSpec artifacts instead.

### Documentation Model (Spec + Context)

- `spec.md` is the **normative SSOT** and should contain only testable requirements.
- Use `openspec/specs/<capability>/context.md` for **free-form context** (purpose, rationale, examples, ops notes).
- If context grows, split into `overview.md`, `rationale.md`, `examples.md`, or `ops.md` within the same capability folder.
- Change-level notes live in `openspec/changes/<change>/context.md` or `notes.md`, then **sync stable context** back into the main context docs.

Prompting cue (use when writing docs):
"Keep `spec.md` strictly for requirements. Add/update `context.md` with purpose, decisions, constraints, failure modes, and at least one concrete example."

### Commands (recommended)

- Start a change: `/opsx:new <kebab-case>`
- Create artifacts (step): `/opsx:continue <change>`
- Create artifacts (fast): `/opsx:ff <change>`
- Implement tasks: `/opsx:apply <change>`
- Verify before archive: `/opsx:verify <change>`
- Sync delta specs → main specs: `/opsx:sync <change>`
- Archive: `/opsx:archive <change>`

## Contributing & Merge Gates

When authoring or merging a PR (as a human contributor, a collaborator,
or an AI assistant acting on behalf of either), the binding workflow is
in [`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md). The sections
an AI assistant most often needs are:

- [Merge gates](.github/CONTRIBUTING.md#merge-gates) — CI green +
  actionable CodeRabbit findings addressed + `mergeable=CLEAN` +
  OpenSpec change folder for behavior changes + `Fixes #N` /
  `Closes #N` for issue cover + the six simplicity rules
  (PRINCIPLES.md P1-P6; see
  [Simplicity gates](.github/CONTRIBUTING.md#simplicity-gates)).
- [Collaborator rules](.github/CONTRIBUTING.md#collaborator-rules) —
  no self-merge by default; large PRs get split (≈1-concern per PR,
  ~800 net lines / scoped capability ceiling).
- [Bus factor escape hatch](.github/CONTRIBUTING.md#bus-factor-escape-hatch)
  — self-merge allowed after **14 days** with all gates met and a
  comment invoking the clause.

An assistant preparing a merge MUST verify the gates against the
actual GitHub state (status check rollup, current-head CodeRabbit review
threads, `mergeable` field) rather than asserting them from local history.
Local `uv run pytest` / `uv run ruff` / `codex review --base origin/main`
are encouraged but not substitutes for the cloud gates.

## PR Readiness / Review Trapdoors

These rules encode recurring review blockers observed across codex-lb PRs.

- OpenSpec is a hard gate for behavior, API, schema, CLI,
  dashboard-visible, proxy-routing, operator-contract, and compatibility
  changes. Create or update `openspec/changes/<slug>/` before coding, keep
  `spec.md` normative with MUST/SHALL-style requirements, put rationale and
  examples in `context.md` or change notes, and run strict OpenSpec validation
  before calling the PR ready. Code/tests alone are not enough when OpenSpec is
  required.
- CodeRabbit review state must come from current-head GitHub evidence.
  Unresolved, non-outdated actionable review threads block readiness until
  their findings are fixed or explicitly addressed or dismissed in-thread;
  a top-level summary does not override active thread evidence.
- Proxy failover and retry patches must prove account ownership and settlement
  invariants. File-pinned requests must not cross accounts; API-key reservations
  must settle before error-health writes; excluded accounts must actually leave
  the selection loop; idle disconnects must not mark otherwise healthy accounts
  unhealthy; security/trusted-access routing must degrade only along the
  documented path.
- Async, fan-out, and session-lifecycle patches must prove task ownership and
  cleanup. Do not share one `AsyncSession` across concurrent tasks; cancel or
  await spawned tasks on failure; preserve finalization/settlement paths after
  partial errors; bound fan-out; and test partial-failure behavior, not only
  the all-success path.
- Database migrations must prove Alembic graph and data hygiene. New revisions
  must sit on the current intended parent with a single-head upgrade path, have
  downgrade/upgrade coverage where the project expects it, and include
  historical-row backfills or compatibility handling when new fields affect
  existing data. Fetch `main` and run `make lint`
  (`scripts/check_migration_topology.py`) after adding a revision: it fails on a
  forked graph, on a revision whose parent `main` has already built on, and on a
  timestamp slot another revision already took.
- Issue-resolving PRs must name the exact `Fixes #N` / `Closes #N`, or state
  that they are partial. Keep PRs one concern wide. Revive stale work by making
  a focused branch on current `main`; do not drag an old broad/conflicted branch
  forward unless the maintainer explicitly wants that shape.
- Bug fixes need regression coverage at the externally failing product path:
  route, bridge, websocket, CLI, schema, dashboard UI, or migration path as
  applicable. Helper-only tests are not enough when the failing surface is
  elsewhere.
- Compatibility work must verify canonical and equivalent paths, trailing slash
  behavior, external error envelopes, env-var semantics, and response-schema
  contracts. Update OpenSpec/context and tests together so docs cannot promise
  behavior the code does not implement.
- Simplicity gates are a merge gate (`PRINCIPLES.md` +
  [CONTRIBUTING.md Simplicity gates](.github/CONTRIBUTING.md#simplicity-gates)).
  New features must default off or work zero-config; new `CODEX_LB_*` settings
  need a why-not-a-default justification in the PR body; README top-level
  sections, `.env.example`, and dashboard core-nav items are budgeted per
  `.github/simplicity-budgets.toml` and exceptions need the maintainer-applied
  `simplicity-budget-approved` label; feature documentation goes to `docs/` +
  openspec (never new README sections); dashboard-visible PRs include
  before/after screenshots.

<!-- BEGIN AI ENGINEERING PACK -->
# Engineering Rules

## Match the requested operation

- Answer questions, explain behavior, review, diagnose, or plan without editing unless the user also
  requests implementation.
- For implementation requests, complete the in-scope code, tests, documentation, and safe local
  verification needed for the requested outcome.
- Do not turn implementation permission into permission to deploy, publish, merge, contact others,
  access production, reveal secrets, perform destructive data operations, or expand scope materially.
- Make reversible assumptions only when they do not change product behavior, architecture, risk,
  cost, or authority. Ask when a missing decision would change any of those.

## Load context when it becomes relevant

- Read the files, callers, tests, contracts, and instructions relevant to the task.
- Read `.ai/PROJECT_CONTEXT.md` for project commands, architecture, invariants, environments, and
  production boundaries.
- Read `.ai/AI_ENGINEERING_GUIDE.md` before consequential implementation, security or data work,
  migrations, dependency changes, cross-module changes, release preparation, or when these rules do
  not resolve a decision.
- For complex features, migrations, multi-module refactors, or multi-stage work, follow
  `.ai/PLANS.md` and maintain the plan in `.ai/plans/`.
- Apply more specific `AGENTS.md` files to work within their directory scope.

## Establish evidence before editing

- Inspect repository status and preserve unrelated work.
- Trace the current behavior through its implementation, callers, tests, contracts, configuration,
  failure paths, and generated outputs.
- Before material work, establish the observable outcome, the mechanism or boundary that owns it,
  and the smallest decisive proof. For a bug or diagnosis, separate symptom from cause and test the
  fact that distinguishes a plausible competing explanation.
- Identify ownership, trust boundaries, public interfaces, compatibility requirements, and the
  authoritative source for generated files.
- Define observable acceptance outcomes, relevant rejection and failure cases, non-goals, and the
  evidence required to verify the result.
- Map every requested outcome to its implementation or answer and evidence. Map every changed
  surface to a requested outcome, a necessary supporting change, or a separately approved decision.

## Implement the smallest complete change

- Preserve established architecture, dependency direction, patterns, and terminology unless the
  requested outcome requires a justified change.
- Keep business rules separate from transport, presentation, persistence, and infrastructure.
- Prefer, in order, no new code, reuse of the responsible repository seam, standard-library or
  native capability, a fitting installed dependency, a small owned implementation, or a coherent
  refactor when existing structure prevents correct ownership.
- Update affected contracts, maintained consumers, tests, documentation, migrations, and generated
  artifacts in the same change.
- Make validation, errors, timeouts, cancellation, retries, concurrency, cleanup, and resource limits
  explicit where they affect behavior.
- Add abstractions and dependencies only for a current requirement. Review compatibility, security,
  licensing, transitive impact, and lockfile changes.
- Optimize total system complexity, ownership, and lifecycle cost, not the number of changed lines
  or files. Do not force a change into the wrong layer to make the diff look smaller.
- Do not weaken tests, assertions, permissions, security controls, or quality gates to obtain a pass.

## Protect the repository and its data

- Validate untrusted input at boundaries and derive identity and authorization from trusted sources.
- Never expose credentials, tokens, personal data, private configuration, or sensitive payloads in
  code, commands, logs, tests, documentation, screenshots, or responses.
- Do not overwrite, revert, reformat, move, or delete unrelated work.
- Use documented generators for derived files and inspect the resulting changes.
- Do not edit lockfiles or generated files manually unless the repository explicitly requires it.
- Resolve and verify exact paths before deletion. Never run destructive Git or filesystem commands
  against work you do not own.
- Do not create commits, branches, tags, pull requests, pushes, merges, releases, or deployments
  unless requested.

## Verify with evidence proportional to risk

- Add or update tests that can fail for the changed behavior.
- Cover applicable success, boundary, rejection, authorization, failure, retry, concurrency,
  accessibility, cleanup, compatibility, and recovery behavior.
- Run the narrowest meaningful check first, then the broader project checks required by the affected
  surfaces and risk.
- Distinguish a passed check, a failed check, an unavailable check, and an outcome blocked by an
  external condition. Never report one as another.
- Re-run affected checks after the last relevant edit.
- Review every final changed file, repository status, generated output, contracts, and documentation.
- If a required tool or environment is unavailable, run safe substitute checks and report the exact
  unverified behavior and command. Never claim an unavailable check passed.

## Report the result

- Lead with the delivered outcome. Remove greetings, execution preambles, repeated summaries, and
  generic offers to continue.
- Preserve commands, paths, identifiers, versions, numbers, constraints, and decisive error text
  exactly. Report the relevant check result instead of dumping noisy or sensitive raw output.
- State important behavior and design decisions, changed files, verification commands and results,
  unavailable checks, assumptions, and remaining risk.
- Distinguish implemented work from recommendations and local evidence from production evidence.
- Treat AI review as advice, not human approval, security certification, compliance acceptance, or
  release authorization.
<!-- END AI ENGINEERING PACK -->
