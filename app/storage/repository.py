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
    last_backup: str | None = None


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
        user_id: int | None = None,
    ) -> list[Expense]:
        """Insert every entry in one transaction. Returns the persisted rows.

        Every row of one message shares a `batch_id`, which is what `/undo`
        operates on -- a three-item message is undone as a unit.
        """
        occurred = (occurred_on or self.today()).isoformat()
        created = datetime.now(UTC).isoformat(timespec="seconds")
        batch_id = str(uuid_lib.uuid4())
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
                user_id=user_id,
                batch_id=batch_id,
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
            self._touch(row)
            row.deleted_at = row.updated_at
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    # --- corrections -------------------------------------------------------

    def soft_delete_batch(self, batch_id: str) -> list[Expense]:
        """Soft-delete every live row of one message. Returns what was removed."""
        with Session(self.engine) as session:
            rows = list(
                session.exec(
                    select(Expense)
                    .where(Expense.batch_id == batch_id)
                    .where(Expense.deleted_at.is_(None))
                    .order_by(Expense.id)
                ).all()
            )
            for row in rows:
                self._touch(row)
                row.deleted_at = row.updated_at
                session.add(row)
            session.commit()
            for row in rows:
                session.refresh(row)
            return rows

    def update_amount(self, row_uuid: str, amount: int) -> Expense | None:
        return self._update(row_uuid, amount=amount)

    def update_category(self, row_uuid: str, category: str) -> Expense | None:
        return self._update(row_uuid, category=category)

    def _update(self, row_uuid: str, **fields) -> Expense | None:
        with Session(self.engine) as session:
            row = session.exec(
                select(Expense)
                .where(Expense.uuid == row_uuid)
                .where(Expense.deleted_at.is_(None))
            ).one_or_none()
            if row is None:
                return None
            for name, value in fields.items():
                setattr(row, name, value)
            self._touch(row)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    @staticmethod
    def _touch(row: Expense) -> None:
        """Stamp a correction and mark the sheet copy stale.

        `sheet_row` is deliberately left alone: `(sheet_synced=0, sheet_row=N)`
        is what tells the sync sweep to update row N in place rather than
        append a second copy. See the PRD's sheet mutation model.
        """
        row.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
        row.sheet_synced = 0

    def clear_sheet_row(self, row_uuid: str) -> None:
        """Forget where a row lives in the sheet, so it is re-appended.

        Called when a row's uuid no longer matches the sheet row we recorded --
        someone reordered or deleted rows by hand.
        """
        with Session(self.engine) as session:
            row = session.exec(
                select(Expense).where(Expense.uuid == row_uuid)
            ).one_or_none()
            if row is None:
                return
            row.sheet_row = None
            row.sheet_synced = 0
            session.add(row)
            session.commit()

    # --- correction targets ------------------------------------------------

    def _owned(self, statement, user_id: int):
        """Restrict a query to rows this owner may correct.

        NULL `user_id` means "logged before multi-owner support existed" and
        stays correctable; rows written since always carry an id, so owners
        cannot reach each other's entries.
        """
        return statement.where(
            (Expense.user_id == user_id) | (Expense.user_id.is_(None))
        ).where(Expense.deleted_at.is_(None))

    def last_entry(self, user_id: int) -> Expense | None:
        """The newest row this owner may correct."""
        with Session(self.engine) as session:
            return session.exec(
                self._owned(select(Expense), user_id)
                .order_by(Expense.id.desc())
                .limit(1)
            ).one_or_none()

    def last_batch(self, user_id: int) -> list[Expense]:
        """Every live row of this owner's newest message.

        Because soft-deleted rows are excluded, repeated `/undo` walks
        backwards through the history for free.
        """
        newest = self.last_entry(user_id)
        if newest is None:
            return []
        if newest.batch_id is None:
            return [newest]
        return self.entries_in_batch(newest.batch_id)

    def get(self, row_uuid: str) -> Expense | None:
        """One live row by uuid, or None if it is missing or deleted."""
        with Session(self.engine) as session:
            return session.exec(
                select(Expense)
                .where(Expense.uuid == row_uuid)
                .where(Expense.deleted_at.is_(None))
            ).one_or_none()

    def entries_in_batch(self, batch_id: str) -> list[Expense]:
        with Session(self.engine) as session:
            return list(
                session.exec(
                    select(Expense)
                    .where(Expense.batch_id == batch_id)
                    .where(Expense.deleted_at.is_(None))
                    .order_by(Expense.id)
                ).all()
            )

    # --- reads -------------------------------------------------------------

    def unsynced(self, limit: int = 200) -> list[Expense]:
        """Rows whose sheet copy is missing or stale.

        A soft-deleted row still needs one last write if it ever reached the
        sheet -- that is how `/undo` flips the sheet's status column to
        `deleted`. A deleted row that was never written has nothing to say.
        """
        with Session(self.engine) as session:
            return list(
                session.exec(
                    select(Expense)
                    .where(Expense.sheet_synced == 0)
                    .where(
                        Expense.deleted_at.is_(None)
                        | Expense.sheet_row.is_not(None)
                    )
                    .order_by(Expense.id)
                    .limit(limit)
                ).all()
            )

    def all_rows(self) -> list[Expense]:
        """Every row, deleted ones included. Used by reconciliation, which has
        to account for the sheet's `status = deleted` rows too."""
        with Session(self.engine) as session:
            return list(session.exec(select(Expense).order_by(Expense.id)).all())

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
                    " WHERE sheet_synced = 0"
                    "   AND (deleted_at IS NULL OR sheet_row IS NOT NULL)"
                )
            ).one()[0]
            parser_rows = session.exec(
                text(
                    "SELECT parser, COUNT(*) FROM expenses"
                    " WHERE deleted_at IS NULL GROUP BY parser"
                )
            ).all()
            last_sync = self._get_state(session, "last_sync")
            last_backup = self._get_state(session, "last_backup")
        return Stats(
            rows=int(rows),
            unsynced=int(unsynced),
            last_sync=last_sync,
            parser_counts={r[0]: int(r[1]) for r in parser_rows},
            last_backup=last_backup,
        )

    # --- sync_state key/value ---------------------------------------------

    def get_state(self, key: str) -> str | None:
        with Session(self.engine) as session:
            return self._get_state(session, key)

    def set_state(self, key: str, value: str) -> None:
        with Session(self.engine) as session:
            self._set_state(session, key, value)
            session.commit()

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
