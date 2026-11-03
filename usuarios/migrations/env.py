import os
from pathlib import Path
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None and config.attributes.get("connection") is None:
    fileConfig(config.config_file_name)

target_metadata = None


def _database_url() -> str:
    database_url_file = os.getenv("DATABASE_URL_FILE")
    database_url = Path(database_url_file).read_text(encoding="utf-8").strip() if database_url_file else os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL es obligatorio para ejecutar migraciones")
    return database_url


def run_migrations_offline():
    context.configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    # shared/run_migrations.py passes its own connection (holding an advisory
    # lock); the caller commits. Direct "alembic upgrade head" builds an engine.
    shared_connection = config.attributes.get("connection")
    if shared_connection is not None:
        context.configure(connection=shared_connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    config.set_main_option("sqlalchemy.url", _database_url().replace("%", "%%"))
    connectable = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
