# Anthropic usage accounting

## Goal
Measured Messages usage reaches downstream key limits, request logs, and existing dashboard rollups.

## Context
The Messages adapter misses nested message_start usage, discards cache reads, and omits all log usage.
Startup failure releases the reservation before failover, so retry success needs a fresh reservation.

## Acceptance outcomes
- Unary and streaming requests persist total input (uncached + cache creation + cache read), output, and cached read counts.
- Cumulative stream updates replace previous values; omitted fields retain previous measurements.
- Observed partial usage survives stream error/disconnect; unknown usage remains null.
- Key reservations settle before health writes; retry success charges exactly once.
- Dashboard raw and folded totals agree with stored usage.

## Non-goals
Claude pricing/catalog replacement, cache-write dollar pricing, UI changes, subscription capacity guesses, real-account access.

## Affected surfaces
Messages adapter, HTTP regression tests, anthropic-proxy-core OpenSpec. Existing schema and rollup readers suffice; no migration or backfill can recover historically discarded measurements. No credentials or payloads logged.

## Design
Own provider normalization in the Anthropic adapter with typed cumulative usage state. Reuse settlement and logging seams. Settle observed partial usage regardless of request outcome, release unknown usage, and renew released reservations before another upstream attempt. Preserve upstream passthrough bytes.

## Implementation stages
1. Extend OpenSpec and add route regressions; demonstrate failure.
2. Normalize usage and propagate into settlement/logs; own stream cleanup and terminal reservation release.
3. Verify focused integration/unit, lint, typecheck, and OpenSpec where available; review diff.

## Verification
HTTP unary/stream, cache-only/zero/missing/invalid counts, cumulative updates, partial errors, failover, reservation state, raw/folded dashboard totals. Existing proxy tests protect framing and failover.

## Rollout and recovery
No migration/settings. Applies to new requests only. Revert adapter changes to roll back; stored totals remain compatible.

## Progress
- [x] Trace provider, settlement, persistence and rollup boundaries.
- [x] Specify and reproduce failures.
- [x] Implement.
- [x] Verify and review (strict OpenSpec validation remains blocked).

## Decisions
- 2026-10-17: Count cache creation as input; preserve cache reads in existing cached-input subset. Do not invent Claude pricing.

## Surprises and discoveries
- Shared settlement detaches by default; health-sensitive calls must explicitly wait.
- Sandbox event-loop wakeups stall even aiosqlite SELECT 1. A temporary `/tmp/claude_lb_sandbox_pytest.py` plugin schedules a 1ms timer; SELECT 1 and regression tests run with it. No repository infrastructure is modified.
- Outside-sandbox verification was not executed: automatic approval review failed because its configured model was unsupported.
- Released reservations cannot later be finalized; renew before retry.

## Evidence
- VERIFIED: saved pre-edit adapter fails the new unary HTTP regression: `(None, None, None) != (15, 2, 7)`.
- VERIFIED: initial six HTTP accounting cases pass, including raw/folded parity (6 passed in 164.00s).
- VERIFIED: full `make lint` and `make typecheck` pass with `UV_CACHE_DIR=/tmp/claude-lb-uv-cache`.
- VERIFIED: final combined run: 84 passed, 2 existing deprecation warnings in 55.50s; includes expanded boundary/disconnect, health ordering, unconfirmed settlement, full Anthropic regressions, existing key settlement and usage summary tests.
- BLOCKED: `openspec validate anthropic-proxy-core --strict` and `openspec validate --specs --strict`: command not found.

## Remaining risk
Claude dollar pricing remains unsupported; previously lost counts cannot be reconstructed.

## Outcomes and retrospective
Measured unary/stream usage now reaches key token limits, request logs and raw/folded dashboard totals.
Partial failures and actual ASGI disconnect preserve usage; startup failover renews reservations;
unconfirmed settlement avoids account penalty. Pre-edit negative control failed as expected, final
84-test run and full lint/typecheck passed. Strict OpenSpec validation remains unavailable, so the
parent change stays open. Existing edits remain preserved. Reusing existing logs/rollups avoided a
migration; a separate pricing change is needed for Claude dollar/cache-write accounting.
