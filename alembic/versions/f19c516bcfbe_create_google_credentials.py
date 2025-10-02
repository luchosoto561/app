from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "f19c516bcfbe"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "google_credentials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("whatsapp_phone", sa.String(length=32), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_type", sa.String(length=20), nullable=False, server_default="Bearer"),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("whatsapp_phone", name="uq_google_credentials_whatsapp_phone"),
    )
    # (Opcional) índice explícito; el Unique ya crea uno, así que no es estrictamente necesario:
    # op.create_index("ix_google_credentials_whatsapp_phone", "google_credentials", ["whatsapp_phone"], unique=True)


def downgrade() -> None:
    # Si alguna vez necesitas bajar, usa IF EXISTS para no romper si ya no está
    op.execute("DROP TABLE IF EXISTS google_credentials CASCADE")
