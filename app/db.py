from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase


def sqlite_engine(db_path: str):
    return create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )


class Base(DeclarativeBase):
    pass


def make_session_factory(engine):
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)
