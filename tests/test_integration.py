"""The whole path a message travels, minus Telegram itself:
parse -> persist -> mirror to Sheets -> show up in the recap.
"""

from app.jobs.daily_recap import build_recap_text
from app.jobs.sync_retry import SyncService
from app.parsing import regex_parser
from tests.test_sync import FakeSheets


async def log_message(repository, sync, text):
    result = regex_parser.parse(text)
    rows = repository.add_many(result.entries, text, result.parser)
    await sync.push(rows)
    return rows


async def test_a_days_messages_become_a_correct_recap(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)

    await log_message(repository, sync, "grab 45k, kopi hitam 20rb")
    await log_message(repository, sync, "makan siang 45k")
    await log_message(repository, sync, "belanja 250.000 di superindo")

    # Everything is in SQLite and mirrored to the sheet.
    assert repository.stats().rows == 4
    assert repository.unsynced() == []
    assert len(sheets.rows) == 4

    recap = build_recap_text(repository, repository.today())
    assert "Rp360.000" in recap  # 45 + 20 + 45 + 250
    assert "Groceries" in recap and "Rp250.000" in recap
    assert "Largest: Rp250.000" in recap


async def test_sheets_outage_is_invisible_to_the_record(repository):
    """The sheet is down for the whole day; not one rupiah is lost."""
    sheets = FakeSheets(fail_times=99)
    sync = SyncService(repository, sheets)

    await log_message(repository, sync, "grab 45k")
    await log_message(repository, sync, "kopi 20k")

    assert sheets.rows == []
    assert repository.stats().rows == 2
    assert "Rp65.000" in build_recap_text(repository, repository.today())

    # Sheets comes back; the retry sweep catches up.
    sheets.fail_times = 0
    assert await sync.flush() == 2
    assert len(sheets.rows) == 2
    assert repository.unsynced() == []


async def test_the_sheet_mirrors_the_database_row_for_row(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    await log_message(repository, sync, "grab 45k, kopi hitam 20rb")

    db_rows = repository.entries_for_day()
    assert [r[0] for r in sheets.rows] == [r.uuid for r in db_rows]
    assert [r[2] for r in sheets.rows] == [r.amount for r in db_rows]
    assert [r[4] for r in sheets.rows] == ["Transport", "Food & Drink"]
