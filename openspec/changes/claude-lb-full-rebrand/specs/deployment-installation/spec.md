# deployment-installation Delta

## MODIFIED Requirements

### Requirement: Data directory resolution follows operator intent

The application MUST resolve its default data directory from operator intent before container heuristics. A non-empty `CLAUDE_LB_DATA_DIR` value MUST be the highest-priority data directory override. When no override is configured, an existing `$HOME/.claude-lb` directory MUST remain preferred even if the process detects that it is running inside a container. The container data directory (`/var/lib/claude-lb`) MUST be used only when no override is configured, the home data directory does not already exist, and container detection is true. There is no compatibility path that reads the legacy `CODEX_LB_DATA_DIR` name or the legacy `~/.codex-lb` directory.

#### Scenario: Explicit override wins

- **GIVEN** `CLAUDE_LB_DATA_DIR` is configured to a non-empty path
- **WHEN** the application resolves its data directory
- **THEN** the configured path is used

#### Scenario: Existing home directory preferred over container path

- **GIVEN** `CLAUDE_LB_DATA_DIR` is not configured
- **AND** `$HOME/.claude-lb` already exists
- **AND** container detection is true
- **THEN** `$HOME/.claude-lb` is used as the data directory
- **AND** `/var/lib/claude-lb` is not selected

#### Scenario: Container default

- **GIVEN** `CLAUDE_LB_DATA_DIR` is not configured
- **AND** `$HOME/.claude-lb` does not exist
- **AND** container detection is true
- **THEN** `/var/lib/claude-lb` is used as the data directory

#### Scenario: Related path overrides remain independent

- **GIVEN** `CLAUDE_LB_DATA_DIR` is configured
- **AND** one or more related paths such as `CLAUDE_LB_DATABASE_URL`, `CLAUDE_LB_ENCRYPTION_KEY_FILE`, or `CLAUDE_LB_CONVERSATION_ARCHIVE_DIR` are explicitly configured
- **WHEN** the application resolves paths
- **THEN** each explicitly configured related path is honored unchanged
