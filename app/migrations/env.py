"""Alembic environment: connects migrations to the app's database and models."""

from alembic import context
from sqlalchemy import create_engine
from sqlmodel import SQLModel

import app.models  # noqa: F401  (registers the tables on SQLModel.metadata)
from app.config import get_config

url = context.config.get_main_option("sqlalchemy.url") or get_config().database_url


def run_migrations() -> None:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=SQLModel.metadata,
            # SQLite can't ALTER most things; batch mode recreates tables instead.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
