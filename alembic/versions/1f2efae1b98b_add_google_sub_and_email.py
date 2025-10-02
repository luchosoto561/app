"""add google_sub and email

Revision ID: 1f2efae1b98b
Revises: f19c516bcfbe
Create Date: 2025-09-22 18:24:48.120662

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1f2efae1b98b'
down_revision: Union[str, Sequence[str], None] = 'f19c516bcfbe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "google_credentials",
        sa.Column("google_sub", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "google_credentials",
        sa.Column("email", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("google_credentials", "email")
    op.drop_column("google_credentials", "google_sub")
