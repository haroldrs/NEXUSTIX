from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from shared.db_engine import engine_options, read_database_url

DATABASE_URL = read_database_url()

engine = create_engine(DATABASE_URL, **engine_options(DATABASE_URL))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
