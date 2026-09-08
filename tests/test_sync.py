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
        self.update_calls = 0

    def _maybe_fail(self):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("Sheets API unavailable")

    async def append(self, expenses):
        self.append_calls += 1
        self._maybe_fail()
        existing = {r[0] for r in self.rows}
        placed = {}
        for expense in expenses:
            if expense.uuid in existing:
                placed[expense.uuid] = 0
                continue
            self.rows.append(expense.sheet_values())
            placed[expense.uuid] = len(self.rows)
        return placed

    async def update(self, expenses):
        """Rewrite rows in place, re-verifying the uuid in column A first."""
        self.update_calls += 1
        self._maybe_fail()
        placed = {}
        for expense in expenses:
            row = expense.sheet_row
            if not row or row > len(self.rows) or self.rows[row - 1][0] != expense.uuid:
                continue  # hand-edited sheet; caller clears sheet_row
            self.rows[row - 1] = expense.sheet_values()
            placed[expense.uuid] = row
        return placed

    async def ensure_status_column(self):
        return None

    async def fetch_rows(self):
        return [list(r) for r in self.rows]

    def cell(self, uuid: str, column: int):
        """The value of one column for one uuid, or None if absent."""
        for row in self.rows:
            if row[0] == uuid:
                return row[column]
        return None


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


# --- the sheet mutation model (PRD 6) ---------------------------------------


async def test_a_correction_updates_the_existing_row_instead_of_appending(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    rows = repository.add_many([entry(45_000)], "grab 45k", "regex")
    await sync.push(rows)
    assert len(sheets.rows) == 1

    repository.update_amount(rows[0].uuid, 145_000)
    assert await sync.flush() == 1

    assert len(sheets.rows) == 1  # updated in place, not duplicated
    assert sheets.cell(rows[0].uuid, 2) == 145_000
    assert repository.unsynced() == []


async def test_a_correction_preserves_sheet_row_so_the_sweep_can_find_it(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    rows = repository.add_many([entry(1_000)], "x", "regex")
    await sync.push(rows)

    updated = repository.update_amount(rows[0].uuid, 2_000)
    assert updated.sheet_row == 1  # the mapping survives the correction
    assert updated.sheet_synced == 0
    assert updated.updated_at is not None


async def test_undo_flips_the_sheet_status_to_deleted(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    rows = repository.add_many([entry(1_000), entry(2_000)], "x", "regex")
    await sync.push(rows)

    removed = repository.soft_delete_batch(rows[0].batch_id)
    assert len(removed) == 2
    assert await sync.flush() == 2

    assert len(sheets.rows) == 2  # rows are mutated, never removed
    assert [r[-1] for r in sheets.rows] == ["deleted", "deleted"]


async def test_a_whole_batch_updates_in_one_api_call(repository):
    """PRD 6: undoing a 5-item message must cost one call, not five."""
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    rows = repository.add_many([entry(1_000)] * 5, "x", "regex")
    await sync.push(rows)

    repository.soft_delete_batch(rows[0].batch_id)
    await sync.flush()

    assert sheets.update_calls == 1


async def test_a_hand_edited_sheet_clears_sheet_row_and_re_appends(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    rows = repository.add_many([entry(1_000)], "x", "regex")
    await sync.push(rows)

    # A human deletes the row in the sheet; our row 1 now holds a stranger.
    sheets.rows[0] = ["someone-elses-uuid"] + sheets.rows[0][1:]
    repository.update_amount(rows[0].uuid, 2_000)

    assert await sync.flush() == 0  # uuid mismatch: nothing overwritten
    assert repository.entries_for_day()[0].sheet_row is None
    assert sheets.rows[0][0] == "someone-elses-uuid"  # untouched

    assert await sync.flush() == 1  # re-appended at the bottom
    assert sheets.cell(rows[0].uuid, 2) == 2_000


async def test_a_correction_during_an_outage_is_repaired_by_the_next_sweep(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    rows = repository.add_many([entry(45_000)], "grab 45k", "regex")
    await sync.push(rows)

    sheets.fail_times = 99
    repository.update_amount(rows[0].uuid, 145_000)
    assert await sync.flush() == 0

    # SQLite is already correct and the row is still dirty.
    assert repository.entries_for_day()[0].amount == 145_000
    assert len(repository.unsynced()) == 1

    sheets.fail_times = 0
    assert await sync.flush() == 1
    assert len(sheets.rows) == 1
    assert sheets.cell(rows[0].uuid, 2) == 145_000
