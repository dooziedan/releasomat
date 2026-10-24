"""global download sources: no per-playlist downloaders

Which download sources are tried, and in which order, is now one global setting for all
playlists (``DOWNLOAD_SOURCES`` in the ``setting`` table, ADR 0008), so ``playlist.downloaders``
is gone. Its old values are not carried over: the global default (Soulseek, then YouTube, with
Soulseek skipped until it is connected) downloads the same way the old default (YouTube) did.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("playlist", schema=None) as batch_op:
        batch_op.drop_column("downloaders")


def downgrade() -> None:
    with op.batch_alter_table("playlist", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("downloaders", sa.VARCHAR(), nullable=False, server_default="youtube")
        )
