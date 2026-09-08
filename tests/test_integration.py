"""The whole path a message travels, minus Telegram itself:
parse -> persist -> mirror to Sheets -> show up in the recap.
"""

import random

import pytest

from app.jobs.daily_recap import build_recap_text
from app.jobs.sync_retry import SyncService
from app.parsing import regex_parser
from tests.test_sync import FakeSheets


async def log_message(repository, sync, text, user_id=None):
    result = regex_parser.parse(text)
    rows = repository.add_many(result.entries, text, result.parser, user_id=user_id)
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
    assert "belanja superindo" in recap and "Rp250.000" in recap
    assert "Total: Rp360.000" in recap


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


# --- corrections end to end (PRD 9) -----------------------------------------


async def test_a_correction_travels_all_the_way_to_the_sheet(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)

    await log_message(repository, sync, "grab 45k, kopi hitam 20rb", user_id=7)

    # Fat-fingered amount, then a wrong category.
    grab = repository.last_batch(7)[0]
    repository.update_amount(grab.uuid, 145_000)
    kopi = repository.last_batch(7)[1]
    repository.update_category(kopi.uuid, "Groceries")
    await sync.flush()

    assert len(sheets.rows) == 2  # corrected in place
    assert sheets.cell(grab.uuid, 2) == 145_000
    assert sheets.cell(kopi.uuid, 4) == "Groceries"

    # The recap re-buckets immediately, because SQLite is the record.
    recap = build_recap_text(repository, repository.today())
    assert "Rp165.000" in recap


async def test_undo_leaves_the_recap_and_the_sheet_agreeing(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)

    keep = await log_message(repository, sync, "makan siang 45k", user_id=7)
    undone = await log_message(repository, sync, "grab 45k, kopi 20k", user_id=7)

    repository.soft_delete_batch(undone[0].batch_id)
    await sync.flush()

    assert "Rp45.000" in build_recap_text(repository, repository.today())
    active = [r for r in sheets.rows if r[-1] == "active"]
    assert [r[0] for r in active] == [keep[0].uuid]
    assert len(sheets.rows) == 3  # nothing was removed from the sheet


@pytest.mark.parametrize("seed", range(12))
async def test_no_sequence_of_corrections_can_duplicate_a_sheet_row(repository, seed):
    """The invariant a correction feature is most likely to break.

    Whatever order the user logs, edits, re-categorises, undoes and syncs in,
    the sheet must hold exactly one row per uuid, and every row must agree
    with SQLite.
    """
    rng = random.Random(seed)
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    messages = ["grab 45k", "kopi 20k, roti 15k", "belanja 250.000", "parkir 5k, tol 12k"]

    for _ in range(30):
        action = rng.choice(
            ["log", "log", "edit", "cat", "undo", "flush", "flush", "outage"]
        )
        if action == "log":
            await log_message(repository, sync, rng.choice(messages), user_id=7)
        elif action == "edit":
            batch = repository.last_batch(7)
            if batch:
                repository.update_amount(
                    rng.choice(batch).uuid, rng.randrange(1_000, 500_000)
                )
        elif action == "cat":
            batch = repository.last_batch(7)
            if batch:
                repository.update_category(rng.choice(batch).uuid, "Groceries")
        elif action == "undo":
            batch = repository.last_batch(7)
            if batch:
                repository.soft_delete_batch(batch[0].batch_id)
        elif action == "outage":
            sheets.fail_times = rng.randrange(1, 3)
        else:
            await sync.flush()

    # Drain whatever is left, then compare the two stores.
    sheets.fail_times = 0
    for _ in range(3):
        await sync.flush()

    uuids = [row[0] for row in sheets.rows]
    assert len(uuids) == len(set(uuids)), "a uuid reached the sheet twice"
    assert repository.unsynced() == []

    # Every live row is present and agrees with SQLite, cell for cell...
    by_uuid = {row[0]: row for row in sheets.rows}
    live = repository.entries_for_day()
    for expense in live:
        assert by_uuid[expense.uuid] == expense.sheet_values()

    # ...and nothing else is still marked active.
    active = {row[0] for row in sheets.rows if row[-1] == "active"}
    assert active == {e.uuid for e in live}
