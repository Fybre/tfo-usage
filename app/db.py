from sqlalchemy import create_engine, inspect, text
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


def sync_schema(engine, base) -> None:
    """Add any columns present in the models but missing from existing tables.

    Base.metadata.create_all() only creates missing tables, not missing columns
    on tables that already exist from an older version of the app. SQLite can
    add nullable columns to an existing table via ALTER TABLE, so this covers
    additive schema changes without needing a full migration tool.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in base.metadata.tables.values():
            if table.name not in existing_tables:
                continue
            existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
