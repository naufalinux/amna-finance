"""SQLite engine setup and hand-rolled migrations.

A `schema_version` table plus an ordered list of DDL steps is enough for v1 --
there is one writer and one deployment, so Alembic would be ceremony.
"""

from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import event, text
from sqlmodel import Session, create_engine

log = structlog.get_logger(__name__)

MIGRATIONS: list[list[str]] = [
    # version 1 -- initial schema
    [
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid          TEXT    NOT NULL UNIQUE,
            occurred_at   TEXT    NOT NULL,
            created_at    TEXT    NOT NULL,
            amount        INTEGER NOT NULL,
            currency      TEXT    NOT NULL DEFAULT 'IDR',
            category      TEXT    NOT NULL,
            note          TEXT,
            raw_message   TEXT    NOT NULL,
            parser        TEXT    NOT NULL,
            confidence    REAL,
            sheet_synced  INTEGER NOT NULL DEFAULT 0,
            sheet_row     INTEGER,
            deleted_at    TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_expenses_occurred "
        "ON expenses(occurred_at) WHERE deleted_at IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_expenses_unsynced "
        "ON expenses(sheet_synced) WHERE sheet_synced = 0",
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            key         TEXT PRIMARY KEY,
            value       TEXT,
            updated_at  TEXT NOT NULL
        )
        """,
    ],
]


def create_db_engine(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", echo=False)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def _current_version(session: Session) -> int:
    session.exec(
        text(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
    )
    session.commit()
    row = session.exec(text("SELECT MAX(version) FROM schema_version")).one()
    return row[0] or 0


def run_migrations(engine) -> int:
    """Apply any migrations newer than the recorded schema version."""
    with Session(engine) as session:
        version = _current_version(session)
        for index, steps in enumerate(MIGRATIONS, start=1):
            if index <= version:
                continue
            for statement in steps:
                session.exec(text(statement))
            session.exec(
                text(
                    "INSERT INTO schema_version (version, applied_at)"
                    " VALUES (:v, :t)"
                ).bindparams(v=index, t=datetime.now(UTC).isoformat(timespec="seconds"))
            )
            session.commit()
            log.info("migration.applied", version=index)
            version = index
        return version


def init_db(db_path: Path):
    engine = create_db_engine(db_path)
    run_migrations(engine)
    return engine
