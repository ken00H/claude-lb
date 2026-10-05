"""rename apply_to_codex_model to apply_to_default_model

Revision ID: 20261005_000000
Revises: 20261001_000000_add_anthropic_account_identity
Create Date: 2026-10-05

claude-lb-full-rebrand: the column's semantic is "rewrite the requested
model to the pool default"; `codex` was upstream branding, not semantics.
Pure rename — no data change.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261005_000000_rename_apply_to_default_model"
down_revision: str | None = "20261001_000000_add_anthropic_account_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("api_keys") as batch:
        batch.alter_column(
            "apply_to_codex_model",
            new_column_name="apply_to_default_model",
        )


def downgrade() -> None:
    with op.batch_alter_table("api_keys") as batch:
        batch.alter_column(
            "apply_to_default_model",
            new_column_name="apply_to_codex_model",
        )
