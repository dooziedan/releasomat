"""Tagwerk integration: adopted and "Don't like" tracks, BPM and key

For ADR 0009 (Releasomat and Tagwerk):

- ``trackfile.former_path``: where a file was in Releasomat's folder before it left (adopted by
  Tagwerk, deleted by the owner, moved away). Indexed: Tagwerk's notices name files by path.
- ``trackfile.adopted_at``, ``disliked_at``, ``disliked_reason``: what happened to the file. The
  new statuses ``adopted`` and ``disliked`` are plain text in ``trackfile.status`` (no schema
  change); these time stamps stay when the status changes (``app/lifecycle.py``).
- ``track.bpm`` and ``track.key``: from playlist sources that know them (Beatport, Tidal), for
  Tagwerk's Releasomat page.

All new columns are nullable, so existing rows need no values.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24 21:16:02.335017
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("track", schema=None) as batch_op:
        batch_op.add_column(sa.Column("bpm", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("key", sqlmodel.sql.sqltypes.AutoString(), nullable=True))

    with op.batch_alter_table("trackfile", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("former_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("adopted_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("disliked_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("disliked_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.create_index(batch_op.f("ix_trackfile_former_path"), ["former_path"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("trackfile", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_trackfile_former_path"))
        batch_op.drop_column("disliked_reason")
        batch_op.drop_column("disliked_at")
        batch_op.drop_column("adopted_at")
        batch_op.drop_column("former_path")

    with op.batch_alter_table("track", schema=None) as batch_op:
        batch_op.drop_column("key")
        batch_op.drop_column("bpm")
