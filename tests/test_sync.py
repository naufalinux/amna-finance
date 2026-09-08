"""Sheets mirroring: failures must never lose a row, retries must not duplicate."""

import pytest

from app.jobs.sync_retry import SyncService
from app.parsing.models import ParsedEntry
from app.storage.sheets import NullSheetsClient


class FakeSheets:
    """Stands in for the Sheets API, with a real uuid-dedupe on column A."""

    enabled = True

    def __init__(self, fail_times: int = 0):
        self.rows: list[list] = []
        self.fail_times = fail_times
        self.append_calls = 0

    async def append(self, expenses):
        self.append_calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("Sheets API unavailable")
        existing = {r[0] for r in self.rows}
        placed = {}
        for expense in expenses:
            if expense.uuid in existing:
                placed[expense.uuid] = 0
                continue
            self.rows.append(expense.sheet_values())
            placed[expense.uuid] = len(self.rows)
        return placed


def entry(amount: int) -> ParsedEntry:
    return ParsedEntry(amount=amount, category="Food & Drink", note="x", confidence=0.95)


async def test_successful_push_marks_rows_synced_with_their_sheet_row(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    rows = repository.add_many([entry(1_000), entry(2_000)], "x", "regex")

    assert await sync.push(rows) == 2
    assert repository.unsynced() == []
    assert len(sheets.rows) == 2
    assert {r.sheet_row for r in repository.entries_for_day()} == {1, 2}


async def test_a_sheets_failure_leaves_the_row_in_sqlite_and_unsynced(repository):
    """PRD 7.4: the user never notices, and the entry is never lost."""
    sync = SyncService(repository, FakeSheets(fail_times=1))
    rows = repository.add_many([entry(1_000)], "x", "regex")

    assert await sync.push(rows) == 0
    assert repository.stats().rows == 1
    assert len(repository.unsynced()) == 1


async def test_the_retry_sweep_flushes_what_the_first_push_missed(repository):
    sheets = FakeSheets(fail_times=1)
    sync = SyncService(repository, sheets)
    rows = repository.add_many([entry(1_000)], "x", "regex")

    await sync.push(rows)  # fails
    assert await sync.flush() == 1  # succeeds

    assert repository.unsynced() == []
    assert len(sheets.rows) == 1


async def test_retrying_an_ambiguous_failure_cannot_duplicate_a_row(repository):
    """The write reached Sheets but the ack didn't reach us -- retry anyway."""
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    rows = repository.add_many([entry(1_000)], "x", "regex")

    await sheets.append(rows)  # the "lost ack" write
    assert len(sheets.rows) == 1

    await sync.flush()  # the retry
    assert len(sheets.rows) == 1  # deduped on uuid
    assert repository.unsynced() == []


async def test_flush_is_a_no_op_when_nothing_is_pending(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    assert await sync.flush() == 0
    assert sheets.append_calls == 0


async def test_soft_deleted_rows_are_never_pushed(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    rows = repository.add_many([entry(1_000)], "x", "regex")
    repository.soft_delete(rows[0].uuid)

    assert await sync.flush() == 0
    assert sheets.rows == []


async def test_push_is_a_no_op_when_sheets_is_not_configured(repository):
    sync = SyncService(repository, NullSheetsClient())
    rows = repository.add_many([entry(1_000)], "x", "regex")

    assert await sync.push(rows) == 0
    assert len(repository.unsynced()) == 1  # still pending, ready if Sheets is added later


async def test_background_push_completes(repository):
    import asyncio

    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    rows = repository.add_many([entry(1_000)], "x", "regex")

    sync.push_in_background(rows)
    await asyncio.sleep(0)
    await asyncio.gather(*sync._background)

    assert len(sheets.rows) == 1
    assert repository.unsynced() == []
