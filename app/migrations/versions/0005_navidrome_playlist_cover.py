"""Navidrome playlist cover: the source's own cover for the Navidrome playlist

For ADR 0011:

- ``playlist.navidrome_cover``: give the Navidrome playlist the source's cover (on by default,
  also for playlists that exist already).
- ``playlist.navidrome_cover_sent``: the cover Releasomat last gave it ("<navidrome id>:<sha256>"),
  so the same picture isn't sent again.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-07 16:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("playlist", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("navidrome_cover", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(
            sa.Column("navidrome_cover_sent", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("playlist", schema=None) as batch_op:
        batch_op.drop_column("navidrome_cover_sent")
        batch_op.drop_column("navidrome_cover")
