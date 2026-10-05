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
