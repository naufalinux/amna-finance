"""Persistence models.

Money is always an integer in minor units (rupiah, cents). Never a float --
`45000`, not `45000.0`. Every layer above this one honours the same rule.
"""

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class Expense(SQLModel, table=True):
    __tablename__ = "expenses"

    id: int | None = Field(default=None, primary_key=True)
    uuid: str = Field(index=True, unique=True)
    occurred_at: str  # ISO8601 date, local-date semantics
    created_at: str  # ISO8601 UTC insert time
    amount: int  # minor units
    currency: str = "IDR"
    category: str
    note: str | None = None
    raw_message: str
    parser: str  # 'regex' | 'llm' | 'manual'
    confidence: float | None = None
    sheet_synced: int = 0
    sheet_row: int | None = None
    deleted_at: str | None = None
    user_id: int | None = None  # Telegram id; NULL = logged pre-multi-owner
    batch_id: str | None = None  # shared by every row of one message
    updated_at: str | None = None  # last correction; NULL = never edited

    @property
    def status(self) -> str:
        """How the row is represented in the sheet's status column."""
        return "deleted" if self.deleted_at else "active"

    def sheet_values(self) -> list[str | int]:
        """Row in the sheet's column order: uuid, date, amount, currency,
        category, note, raw, parser, status."""
        return [
            self.uuid,
            self.occurred_at,
            self.amount,
            self.currency,
            self.category,
            self.note or "",
            self.raw_message,
            self.parser,
            self.status,
        ]


class SyncState(SQLModel, table=True):
    __tablename__ = "sync_state"

    key: str = Field(primary_key=True)
    value: str | None = None
    updated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
