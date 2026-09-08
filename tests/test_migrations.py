"""Migration 2: a v1 database must upgrade in place, once, without data loss."""

from sqlalchemy import text
from sqlmodel import Session

from app.storage.db import MIGRATIONS, create_db_engine, init_db, run_migrations


def columns(engine) -> set[str]:
    with Session(engine) as session:
        return {r[1] for r in session.exec(text("PRAGMA table_info(expenses)")).all()}


def version(engine) -> int:
    with Session(engine) as session:
        return session.exec(text("SELECT MAX(version) FROM schema_version")).one()[0]


def test_a_fresh_database_lands_on_the_latest_version(tmp_path):
    engine = init_db(tmp_path / "fresh.db")
    assert version(engine) == len(MIGRATIONS)
    assert {"user_id", "batch_id", "updated_at"} <= columns(engine)


def test_a_v1_database_upgrades_without_losing_a_row(v1_db_path):
    engine = create_db_engine(v1_db_path)
    assert version(engine) == 1
    assert not {"user_id", "batch_id"} & columns(engine)

    run_migrations(engine)

    assert version(engine) == 2
    assert {"user_id", "batch_id", "updated_at"} <= columns(engine)
    with Session(engine) as session:
        rows = session.exec(
            text("SELECT uuid, amount FROM expenses ORDER BY id")
        ).all()
    assert [(r[0], r[1]) for r in rows] == [
        ("u1", 45_000),
        ("u2", 20_000),
        ("u3", 30_000),
    ]


def test_the_backfill_groups_legacy_rows_by_message(v1_db_path):
    engine = create_db_engine(v1_db_path)
    run_migrations(engine)

    with Session(engine) as session:
        batches = dict(
            session.exec(text("SELECT uuid, batch_id FROM expenses")).all()
        )
    assert all(b is not None for b in batches.values())
    # u1 and u2 came from one message; u3 did not.
    assert batches["u1"] == batches["u2"]
    assert batches["u3"] != batches["u1"]


def test_legacy_rows_keep_a_null_user_id(v1_db_path):
    """NULL means "logged before multi-owner support" -- correctable by anyone."""
    engine = create_db_engine(v1_db_path)
    run_migrations(engine)

    with Session(engine) as session:
        owners = session.exec(text("SELECT DISTINCT user_id FROM expenses")).all()
    assert [o[0] for o in owners] == [None]


def test_running_migrations_again_is_a_no_op(v1_db_path):
    engine = create_db_engine(v1_db_path)
    run_migrations(engine)

    assert run_migrations(engine) == 2  # would raise on a duplicate ADD COLUMN

    with Session(engine) as session:
        applied = session.exec(
            text("SELECT COUNT(*) FROM schema_version WHERE version = 2")
        ).one()[0]
        rows = session.exec(text("SELECT COUNT(*) FROM expenses")).one()[0]
    assert applied == 1
    assert rows == 3
