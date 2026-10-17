# Notes: anthropic-proxy-core

## 2026-10-05 — slice implementation

- Branch: `feat/anthropic-proxy-core`, cut from `chore/close-out-anthropic-account-model`
  (increment-2 close-out commits are a dependency and not yet merged).
- openspec CLI is unavailable on this machine (pre-existing); validation of the
  change folder is pending until the CLI is restored. Format mirrors
  `add-anthropic-account-model`.
- Pre-existing breakage hit by `make test-integration-core`:
  `tests/integration/test_native_sse_egress.py` and
  `test_native_usage_egress.py` read `crates/**` fixtures at collection time;
  `crates/` was deleted in fork increment 1 (task 1.7 removed only the
  `tests/unit` Rust-parity fixtures). These two files belong to the 3.5
  removal scope and are excluded from the local shard runs recorded here.

## Live-verification evidence (pending)

Slots for unparking `add-anthropic-account-model` tasks 1.1/1.2:

- OAuth token exchange capture: NOT YET CAPTURED
  - Token endpoint path observed: `____________`
  - `expires_in`: `____________`
  - Response fields (org/account uuids, scope, plan indicator): `____________`
- Real `/v1/messages` through the proxy: NOT YET CAPTURED
  - Response status + shape: `____________`
  - Rate-limit/subscription response headers (free tier): `____________`

## Usage accounting follow-up (2026-10-17)

The Messages adapter owns normalization into existing logs and key accounting. Cache reads are a
subset of total input; creation tokens also contribute to input. Example: 3 uncached + 5 creation +
7 read = 15 input, 7 cached. Delta usage is cumulative, never additive. Partial usage survives errors
and disconnects; startup retries renew released reservations. Existing rollups need no schema change.
Previously discarded measurements cannot be backfilled. Claude dollar pricing/cache-write rates and
model-catalog replacement remain separate umbrella work; unknown prices remain unknown in logs.

Verification uses the existing HTTP app, real test database, reservations, and hourly folds. The saved
pre-edit adapter fails the new unary regression with `(None, None, None)` instead of `(15, 2, 7)`.
Coverage includes cumulative output, cache-only input, invalid/zero/missing counts, startup failover,
partial failure, native error events, actual ASGI disconnect, upstream closure, committed settlement
before health writes, and unconfirmed settlement without account penalty. The local sandbox stalls
aiosqlite thread wakeups even for in-memory `SELECT 1`; tests use a temporary timer-only pytest plugin
at `/tmp/claude_lb_sandbox_pytest.py`, without repository infrastructure changes.

Reproduce local verification in this environment:

```bash
PYTHONPATH=/tmp:$PWD .venv/bin/python -m pytest -p claude_lb_sandbox_pytest -q \
  tests/integration/test_proxy_messages.py tests/unit/test_anthropic_proxy.py \
  tests/unit/test_proxy_api_key_usage.py tests/integration/test_usage_summary.py \
  --show-capture=no --timeout=45 -n 2
UV_CACHE_DIR=/tmp/claude-lb-uv-cache make lint
UV_CACHE_DIR=/tmp/claude-lb-uv-cache make typecheck
```

Outside this restricted environment the temporary plugin is unnecessary. Strict OpenSpec validation
remains blocked (`openspec: command not found`); no archive or live-account verification is claimed.

Final result: **84 passed, 2 warnings in 55.50s** (existing Starlette/anyio deprecation warnings).
Full `make lint`, `make typecheck`, and `git diff --check` passed. No migration, commit, push,
real-account traffic, or archive was performed. Existing SSE framing edits were preserved.
