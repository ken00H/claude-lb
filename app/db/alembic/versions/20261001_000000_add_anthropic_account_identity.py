"""add anthropic account identity

Revision ID: 20261001_000000
Revises: 20260918_000000_merge_scim_and_overflow_heads
Create Date: 2026-10-01

Additive Anthropic identity columns for add-anthropic-account-model
(claude-lb increment 2). Existing rows backfill to the ``oauth_seat`` pool
class; the id_token becomes nullable because Anthropic's OAuth flow issues
none. ChatGPT-specific columns are dropped in a later increment once the
proxy core rewrite stops reading them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261001_000000_add_anthropic_account_identity"
down_revision: str | None = "20260918_000000_merge_scim_and_overflow_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(
            sa.Column(
                "pool_class",
                sa.String(),
                server_default=sa.text("'oauth_seat'"),
                nullable=False,
            ),
        )
        batch.add_column(sa.Column("anthropic_organization_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("anthropic_account_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("token_expires_at", sa.Integer(), nullable=True))
        # Anthropic issues no id_token; existing ChatGPT rows keep theirs.
        batch.alter_column("id_token_encrypted", existing_type=sa.LargeBinary(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.alter_column("id_token_encrypted", existing_type=sa.LargeBinary(), nullable=False)
        batch.drop_column("token_expires_at")
        batch.drop_column("anthropic_account_id")
        batch.drop_column("anthropic_organization_id")
        batch.drop_column("pool_class")
