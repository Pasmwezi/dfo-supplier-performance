from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .main import database_url_from_env, ensure_bootstrap_admin


def main() -> None:
    database_url = database_url_from_env()
    engine = create_engine(database_url, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    ensure_bootstrap_admin(session_factory, database_url)
    engine.dispose()


if __name__ == "__main__":
    main()
