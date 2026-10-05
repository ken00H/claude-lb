# Tasks: anthropic-proxy-core

## 1. Upstream client

- [x] 1.1 Add Anthropic API protocol constants to `app/core/config/settings.py` (API base URL, messages path, anthropic-version, impersonation headers)
- [x] 1.2 New `app/core/clients/anthropic.py`: `stream_messages()` (SSE) + `request_messages()` (collected) over the route-resolved transport; per-pool-class header builder
- [x] 1.3 Expose test-patchable core-client accessors via the service module façade (`core_stream_messages` / `core_request_messages`)

## 2. Route

- [x] 2.1 `MessagesRequest` schema + Anthropic error envelope helpers (`app/core/anthropic/`)
- [x] 2.2 `POST /v1/messages` handler in `app/modules/proxy/api.py` (validation, request-id, streaming + non-streaming, startup-error probe) and register router in `app/main.py`
- [x] 2.3 Reservation enforcement on the new route; release on every non-consuming path

## 3. Selection + failover

- [x] 3.1 Anthropic service slice under `_service/anthropic.py` (default `_service` domain rules already allow `support` imports; no ratchet edits needed)
- [x] 3.2 Settlement ordering: reservation finalize/release before account health writes on success, failure, and mid-stream death
- [x] 3.3 Classify Anthropic upstream failures (429, overloaded_error/529, 401, 5xx) onto the balancer taxonomy (`rate_limit_error`/`api_error` added to the classifier sets)

## 4. Token freshness

- [x] 4.1 `AuthManager.ensure_fresh`: refresh `oauth_seat` when `token_expires_at` nears (`OAUTH_SEAT_EXPIRY_REFRESH_MARGIN_SECONDS`); `api_key` stays exempt
- [x] 4.2 `account_eligibility.account_access_token_expires_at` reads `token_expires_at` for `oauth_seat` rows

## 5. Rate-limit recording

- [x] 5.1 Log observed `anthropic-ratelimit-*` headers (evidence capture) and thread `retry-after` into the existing rate-limit machinery; no capacity constants (window modeling stays on the umbrella)

## 6. Tests

- [x] 6.1 Integration `tests/integration/test_proxy_messages.py`: non-streaming round trip, streaming order/termination, startup-error probe, invalid payload 400
- [x] 6.2 Failover tests: 429 exclusion + rate-limit marking, 500 failover, mid-stream partial failure (in-stream error event), settlement-before-health-write ordering
- [x] 6.3 Header selection per pool_class; `unknown` plan routable; api_key credential path; mixed-pool failover across classes
- [x] 6.4 Unit tests `tests/unit/test_anthropic_proxy.py`: header builder, failure classification, expiry margin, eligibility column read, envelope conversion

## 7. Gates + evidence

- [ ] 7.1 `make lint`; `make typecheck`; `make test-unit`; integration shards (openspec CLI unavailable locally — validate pending until restored)
- [ ] 7.2 Live verification with a real Claude account: token exchange capture (confirm/flip `OAUTH_TOKEN_PATH`), one real `/v1/messages` through the proxy, record response/rate-limit headers in notes (unparks `add-anthropic-account-model` 1.1/1.2)
- [ ] 7.3 Reconcile umbrella `anthropic-upstream-adaptation/tasks.md` (3.1, 3.2, 3.4, 3.6 done; 3.3 partial)
