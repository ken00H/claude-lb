# anthropic-accounts — Spec Delta

## ADDED Requirements

### Requirement: Two upstream pool classes

The system SHALL model upstream accounts with a `pool_class` of `oauth_seat` (claude.ai
subscription credential with refreshable tokens) or `api_key` (static Anthropic API
credential). Both classes SHALL live in the `accounts` table, share one eligibility and
selection path, and be distinguishable by every consumer of the account record.

#### Scenario: Mixed pool selection

- **WHEN** the pool contains one `oauth_seat` account and one `api_key` account, both active
- **THEN** both are visible to the account-selection path as accounts, each carrying its `pool_class`

### Requirement: OAuth seat credential lifecycle

The system SHALL obtain OAuth seat credentials via the Anthropic OAuth flow (PKCE S256,
copy/paste callback supported), store the access and refresh tokens encrypted, and SHALL
refresh an access token using the stored refresh token before it expires. If a refresh
response omits a refresh token, the system SHALL retain the previously stored refresh
token. An OAuth seat whose refresh token is absent or whose refresh fails with a
non-retryable error SHALL transition to `reauth_required`, not be deleted.

#### Scenario: Refresh response without rotation

- **WHEN** a refresh exchange returns a new access token but no `refresh_token` field
- **THEN** the previously stored refresh token remains stored and the account stays active

### Requirement: API-key account import

The system SHALL accept an Anthropic API key (`sk-ant-` prefixed) as an account
credential without any OAuth flow. API-key accounts SHALL be excluded from token-refresh
processing and SHALL be routable immediately after validation.

#### Scenario: Import and immediate routability

- **WHEN** an operator submits a valid `sk-ant-…` key through the dashboard import path
- **THEN** an `api_key` account row exists in active status and no token-refresh job targets it

### Requirement: Unknown plan is routable

An account whose plan cannot be determined (`unknown`) SHALL remain eligible for
selection. Plan detection failure SHALL NOT disable an account.

#### Scenario: Plan detection failure

- **WHEN** profile/organization lookup returns no recognizable plan for an active OAuth seat
- **THEN** the account's plan is stored as `unknown` and the account remains eligible

### Requirement: Anthropic identity fields

Each account SHALL record, when obtainable: `anthropic_organization_id`,
`anthropic_account_id`, an optional email, and a user-supplied alias. The system SHALL
NOT require ChatGPT-specific identity fields. Missing optional identity fields SHALL NOT
block routing.

#### Scenario: Token response without profile email

- **WHEN** an OAuth exchange persists credentials but no email is resolvable
- **THEN** the account row persists with a null email and remains routable

### Requirement: Manual callback as first-class flow

The OAuth start flow SHALL provide a manual-callback path in which the operator pastes
the browser redirect result (including `CODE#STATE` form) into the dashboard, and that
path SHALL complete the same PKCE exchange and persistence as the automatic path.

#### Scenario: Pasted CODE#STATE value

- **WHEN** the operator pastes a redirect result of the form `CODE#STATE` into the manual-callback route
- **THEN** the system splits the value, completes the PKCE exchange with the stored verifier, and persists the resulting credentials
