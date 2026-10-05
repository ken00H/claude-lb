# configuration-tiers Delta

## MODIFIED Requirements

### Requirement: Environment variable prefix

Every application setting resolves from the environment under the single prefix `CLAUDE_LB_`, declared once as the `env_prefix` of the `Settings` model in `app/core/config/settings.py`. No setting may be read under the legacy `CODEX_LB_` prefix and no compatibility alias between the two prefixes MAY exist. Where any other requirement of this capability, an operator document, or the generated settings reference names a prefixed variable, the `CLAUDE_LB_` form is the canonical name.

#### Scenario: Prefix is declared once

- **WHEN** the `Settings` model is inspected
- **THEN** its `env_prefix` equals `CLAUDE_LB_`

#### Scenario: Legacy prefix is not read

- **GIVEN** only `CODEX_LB_DATA_DIR` is set in the environment
- **WHEN** `Settings` is constructed
- **THEN** the legacy variable is ignored and the code default applies

### Requirement: `.env.example` lists only bootstrap and topology settings

`.env.example` SHALL mention only settings tiered `T0` or `T1` (`PORT` and non-setting text are unaffected). `scripts/check_settings_tiers.py` SHALL fail when a `CLAUDE_LB_*` variable in `.env.example`, commented or not, resolves to a T2, T3, or T4 field.

#### Scenario: Tunable leaked into `.env.example`

- **WHEN** a PR adds `# CLAUDE_LB_<T3_FIELD>=...` to `.env.example`
- **THEN** `make lint` fails naming the variable

### Requirement: Direct environment reads are confined to the settings module

Under `app/`, `os.environ`, `os.getenv` and `dotenv_values` MUST be referenced only in `app/core/config/settings.py`. Every `CLAUDE_LB_*` variable the application consumes MUST be a `Settings` field with a declared tier, so that it appears in the generated settings reference and is covered by the removed-settings warning when retired. The only exception is the allowlist `ENV_READ_ALLOWLIST` in `scripts/check_settings_tiers.py`, which names, per file, the number of lines that read the environment and the variables they read.

#### Scenario: Prefixed variable outside Settings

- **WHEN** a PR reads a `CLAUDE_LB_*` variable through `os.environ` outside `app/core/config/settings.py` without an allowlist entry
- **THEN** `make lint` fails naming the file and the variable
