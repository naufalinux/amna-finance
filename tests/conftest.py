from pathlib import Path

import pytest
import yaml

FIXTURES = Path(__file__).parent / "fixtures"


def load_messages() -> list[dict]:
    return yaml.safe_load((FIXTURES / "messages.yaml").read_text())


@pytest.fixture
def repository(tmp_path):
    from app.storage.db import init_db
    from app.storage.repository import Repository

    return Repository(init_db(tmp_path / "test.db"), timezone="Asia/Jakarta")


@pytest.fixture
def v1_db_path(tmp_path):
    """A database at schema version 1, ready to be upgraded.

    Migration 2 has to be provable against a real pre-upgrade file, so this
    applies only the first migration and records it exactly as the runner
    would, then seeds two messages' worth of rows.
    """
    from sqlalchemy import text
    from sqlmodel import Session

    from app.storage.db import MIGRATIONS, create_db_engine

    db_path = tmp_path / "v1.db"
    engine = create_db_engine(db_path)
    with Session(engine) as session:
        session.exec(
            text(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                " version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
            )
        )
        for statement in MIGRATIONS[0]:
            session.exec(text(statement))
        session.exec(
            text(
                "INSERT INTO schema_version (version, applied_at)"
                " VALUES (1, '2026-09-01T00:00:00')"
            )
        )
        # Two rows from one message, one row from another. Under v1 the only
        # thing tying a message together is (created_at, raw_message).
        rows = [
            ("u1", "2026-09-01T10:00:00", "grab 45k, kopi 20k", 45_000, "Transport"),
            ("u2", "2026-09-01T10:00:00", "grab 45k, kopi 20k", 20_000, "Food & Drink"),
            ("u3", "2026-09-01T11:00:00", "makan 30k", 30_000, "Food & Drink"),
        ]
        for uuid, created, raw, amount, category in rows:
            session.exec(
                text(
                    "INSERT INTO expenses (uuid, occurred_at, created_at, amount,"
                    " currency, category, raw_message, parser)"
                    " VALUES (:u, '2026-09-01', :c, :a, 'IDR', :cat, :raw, 'regex')"
                ).bindparams(u=uuid, c=created, a=amount, cat=category, raw=raw)
            )
        session.commit()
    engine.dispose()
    return db_path
