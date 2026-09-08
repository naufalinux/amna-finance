"""Write-through facade over SQLite. The only storage API the app uses.

Writes land in SQLite synchronously (instant, always succeeds) and the Sheets
mirror is pushed afterwards by the caller. Nothing above this layer touches SQL.
"""

import uuid as uuid_lib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session, select

from app.parsing.models import ParsedEntry
from app.storage.models import Expense


@dataclass(frozen=True)
class CategoryTotal:
    category: str
    total: int
    count: int


@dataclass(frozen=True)
class Stats:
    rows: int
    unsynced: int
    last_sync: str | None
    parser_counts: dict[str, int]


class Repository:
    def __init__(self, engine, timezone: str = "Asia/Jakarta"):
        self.engine = engine
        self.tz = ZoneInfo(timezone)

    # --- clock -------------------------------------------------------------

    def today(self) -> date:
        return datetime.now(self.tz).date()

    # --- writes ------------------------------------------------------------

    def add_many(
        self,
        entries: list[ParsedEntry],
        raw_message: str,
        parser: str,
        occurred_on: date | None = None,
    ) -> list[Expense]:
        """Insert every entry in one transaction. Returns the persisted rows."""
        occurred = (occurred_on or self.today()).isoformat()
        created = datetime.now(UTC).isoformat(timespec="seconds")
        rows = [
            Expense(
                uuid=str(uuid_lib.uuid4()),
                occurred_at=occurred,
                created_at=created,
                amount=entry.amount,
                currency=entry.currency,
                category=entry.category or "Uncategorized",
                note=entry.note or None,
                raw_message=raw_message,
                parser=parser,
                confidence=entry.confidence,
            )
            for entry in entries
        ]
        with Session(self.engine) as session:
            for row in rows:
                session.add(row)
            session.commit()
            for row in rows:
                session.refresh(row)
        return rows

    def mark_synced(self, row_uuid: str, sheet_row: int | None) -> None:
        with Session(self.engine) as session:
            row = session.exec(
                select(Expense).where(Expense.uuid == row_uuid)
            ).one_or_none()
            if row is None:
                return
            row.sheet_synced = 1
            row.sheet_row = sheet_row
            session.add(row)
            self._set_state(session, "last_sync", datetime.now(UTC).isoformat(timespec="seconds"))
            session.commit()

    def soft_delete(self, row_uuid: str) -> Expense | None:
        with Session(self.engine) as session:
            row = session.exec(
                select(Expense).where(Expense.uuid == row_uuid)
            ).one_or_none()
            if row is None:
                return None
            row.deleted_at = datetime.now(UTC).isoformat(timespec="seconds")
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    # --- reads -------------------------------------------------------------

    def unsynced(self, limit: int = 200) -> list[Expense]:
        with Session(self.engine) as session:
            return list(
                session.exec(
                    select(Expense)
                    .where(Expense.sheet_synced == 0)
                    .where(Expense.deleted_at.is_(None))
                    .order_by(Expense.id)
                    .limit(limit)
                ).all()
            )

    def entries_for_day(self, day: date | None = None) -> list[Expense]:
        return self.entries_between(day or self.today(), day or self.today())

    def entries_between(self, start: date, end: date) -> list[Expense]:
        with Session(self.engine) as session:
            return list(
                session.exec(
                    select(Expense)
                    .where(Expense.occurred_at >= start.isoformat())
                    .where(Expense.occurred_at <= end.isoformat())
                    .where(Expense.deleted_at.is_(None))
                    .order_by(Expense.occurred_at, Expense.id)
                ).all()
            )

    def totals_by_category(self, start: date, end: date) -> list[CategoryTotal]:
        sql = text(
            "SELECT category, SUM(amount) AS total, COUNT(*) AS n"
            " FROM expenses"
            " WHERE occurred_at >= :start AND occurred_at <= :end"
            "   AND deleted_at IS NULL"
            " GROUP BY category ORDER BY total DESC"
        )
        with Session(self.engine) as session:
            rows = session.exec(
                sql.bindparams(start=start.isoformat(), end=end.isoformat())
            ).all()
        return [CategoryTotal(r[0], int(r[1]), int(r[2])) for r in rows]

    def total_between(self, start: date, end: date) -> int:
        sql = text(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses"
            " WHERE occurred_at >= :start AND occurred_at <= :end"
            "   AND deleted_at IS NULL"
        )
        with Session(self.engine) as session:
            return int(
                session.exec(
                    sql.bindparams(start=start.isoformat(), end=end.isoformat())
                ).one()[0]
            )

    def daily_average(self, end: date, days: int = 7) -> float:
        """Mean daily spend over the `days` days ending the day before `end`."""
        start = end - timedelta(days=days)
        prior_end = end - timedelta(days=1)
        if prior_end < start:
            return 0.0
        return self.total_between(start, prior_end) / days

    def stats(self) -> Stats:
        with Session(self.engine) as session:
            rows = session.exec(
                text("SELECT COUNT(*) FROM expenses WHERE deleted_at IS NULL")
            ).one()[0]
            unsynced = session.exec(
                text(
                    "SELECT COUNT(*) FROM expenses"
                    " WHERE sheet_synced = 0 AND deleted_at IS NULL"
                )
            ).one()[0]
            parser_rows = session.exec(
                text(
                    "SELECT parser, COUNT(*) FROM expenses"
                    " WHERE deleted_at IS NULL GROUP BY parser"
                )
            ).all()
            last_sync = self._get_state(session, "last_sync")
        return Stats(
            rows=int(rows),
            unsynced=int(unsynced),
            last_sync=last_sync,
            parser_counts={r[0]: int(r[1]) for r in parser_rows},
        )

    # --- sync_state key/value ---------------------------------------------

    @staticmethod
    def _get_state(session: Session, key: str) -> str | None:
        row = session.exec(
            text("SELECT value FROM sync_state WHERE key = :k").bindparams(k=key)
        ).one_or_none()
        return row[0] if row else None

    @staticmethod
    def _set_state(session: Session, key: str, value: str) -> None:
        session.exec(
            text(
                "INSERT INTO sync_state (key, value, updated_at)"
                " VALUES (:k, :v, :t)"
                " ON CONFLICT(key) DO UPDATE SET value = :v, updated_at = :t"
            ).bindparams(k=key, v=value, t=datetime.now(UTC).isoformat(timespec="seconds"))
        )
