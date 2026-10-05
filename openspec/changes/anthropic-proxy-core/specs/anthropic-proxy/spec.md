# anthropic-proxy — Spec Delta

## ADDED Requirements

### Requirement: Messages endpoint

The system SHALL expose `POST /v1/messages` implementing the Anthropic Messages API request shape
(model, messages, max_tokens, stream) with request validation, request-id handling, SSE streaming and
non-streaming responses. The endpoint SHALL be guarded by the proxy API-key authentication used by the
other proxy surfaces. Downstream error responses SHALL use the Anthropic error envelope
(`{"type": "error", "error": {"type", "message"}}`).

#### Scenario: Non-streaming round trip

- **WHEN** a client posts a valid non-streaming Messages request to `/v1/messages`
- **THEN** the response body is the upstream Anthropic Messages response passed through unchanged in
  shape, with upstream usage present

#### Scenario: Invalid payload rejected before account selection

- **WHEN** the payload fails validation (missing `model`, empty `messages`, or missing `max_tokens`)
- **THEN** the endpoint responds 400 with an Anthropic error envelope and no account is selected and no
  reservation is taken

### Requirement: Streaming passthrough preserves ordering

The system SHALL stream upstream Server-Sent Events to the downstream client preserving event order and
terminations. The system SHALL NOT buffer and re-emit the stream. Pre-first-event upstream failures
SHALL surface as ordinary HTTP error responses.

#### Scenario: Stream termination delivered

- **WHEN** the upstream stream ends with a `message_stop` event
- **THEN** the downstream client receives all prior events in order followed by that `message_stop`

#### Scenario: Startup failure is an HTTP error

- **WHEN** the upstream returns an error response before any stream event is emitted
- **THEN** the downstream client receives the mapped HTTP status and Anthropic error envelope instead of
  a 200 stream

### Requirement: Per-pool-class upstream authentication

The system SHALL authenticate upstream Messages requests by the selected account's `pool_class`:
`oauth_seat` accounts SHALL send `Authorization: Bearer` with the stored access token plus the Claude
Code client impersonation headers; `api_key` accounts SHALL send `x-api-key` with the stored key plus
`anthropic-version`. The request SHALL NOT send API-key credentials for OAuth seats or OAuth credentials
for API keys.

#### Scenario: OAuth seat request headers

- **WHEN** a request is served through an `oauth_seat` account
- **THEN** the upstream request carries `Authorization: Bearer <access-token>` and the impersonation
  headers, and no `x-api-key`

#### Scenario: API-key request headers

- **WHEN** a request is served through an `api_key` account
- **THEN** the upstream request carries `x-api-key: <key>` and `anthropic-version`, and no
  `Authorization: Bearer`

### Requirement: Failover preserves ownership and settlement invariants

The system SHALL reuse the existing account selection and failover machinery: failed account attempts
SHALL be excluded from subsequent selection attempts, upstream API-key reservations SHALL settle before
account error-health writes on every request path, and failover SHALL be bounded. Requests SHALL NOT
fail over across an ownership boundary (this slice introduces no affinity sources, so pre-first-token
failover is always safe).

#### Scenario: Excluded account leaves the loop

- **WHEN** the first selected account fails with a failover-eligible upstream error
- **THEN** the retry selects a different account and the failed account id is in the exclusion set of
  that selection

#### Scenario: Reservation settles before health write

- **WHEN** a request that took an upstream API-key reservation ends in an account-health-affecting error
- **THEN** the reservation is finalized or released before the account health write is recorded

### Requirement: OAuth seat token freshness

The system SHALL treat an `oauth_seat` access token as stale and refresh it before use when the stored
`token_expires_at` is within a safety margin of the current time, in addition to existing refresh
triggers. Token expiry SHALL be evaluated from the `token_expires_at` column for `oauth_seat` accounts;
JWT-claim parsing SHALL NOT be the only expiry signal.

#### Scenario: Expiring token refreshed before request

- **WHEN** a request targets an `oauth_seat` whose `token_expires_at` is minutes away
- **THEN** the account's access token is refreshed before the upstream call uses it

#### Scenario: Expired reauth seat not routable

- **WHEN** an `oauth_seat` is in `reauth_required` status and its `token_expires_at` has passed
- **THEN** the account is not selected for new requests

### Requirement: API-key seats stay refresh-exempt

The system SHALL NOT run token refresh for `api_key` pool accounts on any request path or maintenance
sweep touched by this slice.

#### Scenario: API-key account never refreshed

- **WHEN** a request is served through an `api_key` account
- **THEN** no refresh exchange is initiated for that account

### Requirement: Rate-limit headers recorded without invented capacities

The system SHALL record upstream rate-limit response headers (`anthropic-ratelimit-*` for API keys, and
any subscription limit headers observed) into existing usage/rate-limit fields where they fit. The
system SHALL NOT model 5-hour or weekly window capacities in this slice; 429 responses SHALL flow
through the existing rate-limit health machinery.

#### Scenario: API-key rate-limit headers stored

- **WHEN** an upstream response carries `anthropic-ratelimit-requests-limit` and related headers
- **THEN** the values are recorded on the account's usage state where the existing fields accept them,
  and no capacity constant is introduced
