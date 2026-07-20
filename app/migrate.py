from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from .main import database_url_from_env
from .models import Base


def main() -> None:
    database_url = database_url_from_env()
    engine = create_engine(database_url, pool_pre_ping=True)
    config_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = Config(str(config_path))

    with engine.begin() as connection:
        existing_tables = set(inspect(connection).get_table_names())
        config.attributes["connection"] = connection
        if existing_tables and "alembic_version" not in existing_tables:
            missing = set(Base.metadata.tables) - existing_tables
            if missing:
                names = ", ".join(sorted(missing))
                raise RuntimeError(f"Refusing to stamp an incomplete legacy schema; missing tables: {names}")
            command.stamp(config, "head")
        command.upgrade(config, "head")
    engine.dispose()


if __name__ == "__main__":
    main()
