# telemetry Delta

## MODIFIED Requirements

### Requirement: Telemetry identity names the product

The telemetry client MUST report `app_name` as the literal `"claude-lb"`. The consent environment fallback is named `CLAUDE_LB_TELEMETRY_ENABLED` (T3, resolved through the standard dashboard-wins precedence shared with `dashboard_settings.telemetry_consent`); there is no compatibility read of the legacy `CODEX_LB_TELEMETRY_ENABLED` name.

#### Scenario: App name in telemetry payloads

- **WHEN** a telemetry event is emitted
- **THEN** its `app_name` field equals `claude-lb`

#### Scenario: Consent environment fallback

- **GIVEN** no telemetry decision is saved in the dashboard
- **WHEN** `CLAUDE_LB_TELEMETRY_ENABLED=false` is set in the environment
- **THEN** telemetry stays disabled until a dashboard decision is saved
