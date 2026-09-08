from datetime import date, timedelta

import pytest

from app.parsing.models import ParsedEntry


def entry(amount: int, category: str = "Food & Drink", note: str = "x") -> ParsedEntry:
    return ParsedEntry(amount=amount, category=category, note=note, confidence=0.95)


def test_add_many_persists_every_entry_in_one_go(repository):
    rows = repository.add_many(
        [entry(45_000, "Transport", "grab"), entry(20_000, "Food & Drink", "kopi")],
        raw_message="grab 45k, kopi 20k",
        parser="regex",
    )
    assert len(rows) == 2
    assert {r.amount for r in rows} == {45_000, 20_000}
    assert all(r.uuid for r in rows)
    assert len({r.uuid for r in rows}) == 2
    assert all(r.sheet_synced == 0 for r in rows)
    assert all(r.raw_message == "grab 45k, kopi 20k" for r in rows)


def test_amounts_stay_integers(repository):
    rows = repository.add_many([entry(45_000)], "x", "regex")
    assert isinstance(rows[0].amount, int)


def test_new_rows_are_unsynced_until_marked(repository):
    rows = repository.add_many([entry(1_000), entry(2_000)], "x", "regex")
    assert len(repository.unsynced()) == 2

    repository.mark_synced(rows[0].uuid, 5)
    pending = repository.unsynced()
    assert [p.uuid for p in pending] == [rows[1].uuid]

    stats = repository.stats()
    assert stats.unsynced == 1
    assert stats.last_sync is not None


def test_mark_synced_records_the_sheet_row(repository):
    row = repository.add_many([entry(1_000)], "x", "regex")[0]
    repository.mark_synced(row.uuid, 7)
    assert repository.unsynced() == []
    day = repository.entries_for_day()
    assert day[0].sheet_row == 7


def test_mark_synced_on_an_unknown_uuid_is_a_no_op(repository):
    repository.mark_synced("does-not-exist", 1)
    assert repository.stats().rows == 0


def test_soft_deleted_rows_disappear_from_reads(repository):
    rows = repository.add_many([entry(1_000), entry(2_000)], "x", "regex")
    repository.soft_delete(rows[0].uuid)

    assert [r.uuid for r in repository.entries_for_day()] == [rows[1].uuid]
    assert repository.total_between(repository.today(), repository.today()) == 2_000
    assert repository.stats().rows == 1
    assert [t.total for t in repository.totals_by_category(repository.today(), repository.today())] == [2_000]


def test_totals_by_category_groups_and_sorts_by_size(repository):
    repository.add_many(
        [
            entry(50_000, "Food & Drink"),
            entry(62_000, "Food & Drink"),
            entry(85_000, "Transport"),
        ],
        "x",
        "regex",
    )
    today = repository.today()
    totals = repository.totals_by_category(today, today)
    assert [(t.category, t.total, t.count) for t in totals] == [
        ("Food & Drink", 112_000, 2),
        ("Transport", 85_000, 1),
    ]


def test_date_range_reads_exclude_days_outside_the_window(repository):
    today = repository.today()
    repository.add_many([entry(10_000)], "x", "regex", occurred_on=today)
    repository.add_many([entry(99_000)], "x", "regex", occurred_on=today - timedelta(days=3))

    assert repository.total_between(today, today) == 10_000
    assert repository.total_between(today - timedelta(days=7), today) == 109_000
    assert len(repository.entries_for_day(today - timedelta(days=3))) == 1


def test_daily_average_covers_prior_days_only(repository):
    today = repository.today()
    # 70.000 spread over the 7 days before today, plus a big spend today.
    for offset in range(1, 8):
        repository.add_many([entry(10_000)], "x", "regex", occurred_on=today - timedelta(days=offset))
    repository.add_many([entry(500_000)], "x", "regex", occurred_on=today)

    assert repository.daily_average(today, days=7) == pytest.approx(10_000)


def test_daily_average_is_zero_with_no_history(repository):
    assert repository.daily_average(repository.today(), days=7) == 0.0


def test_stats_counts_rows_per_parser(repository):
    repository.add_many([entry(1_000)], "x", "regex")
    repository.add_many([entry(2_000), entry(3_000)], "y", "llm")
    assert repository.stats().parser_counts == {"regex": 1, "llm": 2}


def test_persistence_survives_a_new_repository_on_the_same_file(tmp_path):
    from app.storage.db import init_db
    from app.storage.repository import Repository

    path = tmp_path / "persist.db"
    Repository(init_db(path)).add_many([entry(45_000)], "grab 45k", "regex")

    reopened = Repository(init_db(path))
    assert reopened.stats().rows == 1
    assert reopened.entries_for_day()[0].amount == 45_000


def test_sheet_values_match_the_sheet_column_order(repository):
    row = repository.add_many([entry(45_000, "Transport", "grab")], "grab 45k", "regex")[0]
    values = row.sheet_values()
    assert values[0] == row.uuid
    assert values[1] == row.occurred_at
    assert values[2] == 45_000
    assert values[3] == "IDR"
    assert values[4] == "Transport"
    assert values[5] == "grab"
    assert values[6] == "grab 45k"
    assert values[7] == "regex"


# --- corrections (PRD 7.1) --------------------------------------------------


def test_every_row_of_one_message_shares_a_batch_id(repository):
    rows = repository.add_many([entry(1_000), entry(2_000)], "a, b", "regex")
    other = repository.add_many([entry(3_000)], "c", "regex")

    assert rows[0].batch_id == rows[1].batch_id
    assert other[0].batch_id != rows[0].batch_id


def test_last_batch_returns_the_whole_newest_message(repository):
    repository.add_many([entry(1_000)], "old", "regex", user_id=7)
    newest = repository.add_many([entry(2_000), entry(3_000)], "new", "regex", user_id=7)

    found = repository.last_batch(7)
    assert [r.uuid for r in found] == [r.uuid for r in newest]


def test_last_entry_is_the_newest_single_row(repository):
    repository.add_many([entry(1_000)], "old", "regex", user_id=7)
    newest = repository.add_many([entry(2_000), entry(3_000)], "new", "regex", user_id=7)

    assert repository.last_entry(7).uuid == newest[-1].uuid


def test_one_owner_cannot_reach_another_owners_entry(repository):
    """PRD 7.1: my /undo never touches my partner's last message."""
    mine = repository.add_many([entry(1_000)], "mine", "regex", user_id=7)
    repository.add_many([entry(2_000)], "theirs", "regex", user_id=9)

    assert [r.uuid for r in repository.last_batch(7)] == [mine[0].uuid]
    assert repository.last_entry(7).uuid == mine[0].uuid


def test_a_legacy_row_with_no_owner_stays_correctable(repository):
    legacy = repository.add_many([entry(1_000)], "legacy", "regex")

    assert repository.last_entry(7).uuid == legacy[0].uuid
    assert repository.last_entry(9).uuid == legacy[0].uuid


def test_repeated_undo_walks_backwards_through_the_messages(repository):
    first = repository.add_many([entry(1_000)], "first", "regex", user_id=7)
    second = repository.add_many([entry(2_000)], "second", "regex", user_id=7)
    third = repository.add_many([entry(3_000)], "third", "regex", user_id=7)

    for expected in (third, second, first):
        batch = repository.last_batch(7)
        assert [r.uuid for r in batch] == [r.uuid for r in expected]
        repository.soft_delete_batch(batch[0].batch_id)

    assert repository.last_batch(7) == []  # nothing left to undo


def test_soft_delete_batch_removes_every_item_of_the_message(repository):
    rows = repository.add_many([entry(1_000), entry(2_000), entry(3_000)], "x", "regex")

    removed = repository.soft_delete_batch(rows[0].batch_id)

    assert len(removed) == 3
    assert all(r.deleted_at is not None for r in removed)
    assert repository.entries_for_day() == []


def test_soft_delete_batch_is_idempotent(repository):
    rows = repository.add_many([entry(1_000)], "x", "regex")
    repository.soft_delete_batch(rows[0].batch_id)

    assert repository.soft_delete_batch(rows[0].batch_id) == []


def test_update_amount_corrects_the_row_and_marks_the_mirror_stale(repository):
    rows = repository.add_many([entry(45_000)], "grab 45k", "regex")
    repository.mark_synced(rows[0].uuid, 4)

    updated = repository.update_amount(rows[0].uuid, 145_000)

    assert updated.amount == 145_000
    assert updated.updated_at is not None
    assert updated.sheet_synced == 0
    assert updated.sheet_row == 4  # the sheet mapping must survive


def test_update_category_corrects_the_row(repository):
    rows = repository.add_many([entry(15_000)], "titip 15k", "regex")

    updated = repository.update_category(rows[0].uuid, "Groceries")

    assert updated.category == "Groceries"
    assert updated.sheet_synced == 0


def test_correcting_a_deleted_row_does_nothing(repository):
    rows = repository.add_many([entry(1_000)], "x", "regex")
    repository.soft_delete(rows[0].uuid)

    assert repository.update_amount(rows[0].uuid, 2_000) is None
    assert repository.update_category(rows[0].uuid, "Groceries") is None


def test_correcting_an_unknown_uuid_does_nothing(repository):
    assert repository.update_amount("nope", 1_000) is None
    assert repository.update_category("nope", "Groceries") is None


def test_a_deleted_row_that_reached_the_sheet_is_still_dirty(repository):
    """/undo has to tell the sheet, so the row stays in the sync queue."""
    rows = repository.add_many([entry(1_000)], "x", "regex")
    repository.mark_synced(rows[0].uuid, 3)
    repository.soft_delete(rows[0].uuid)

    assert [r.uuid for r in repository.unsynced()] == [rows[0].uuid]
    assert repository.stats().unsynced == 1


def test_a_deleted_row_that_never_reached_the_sheet_has_nothing_to_say(repository):
    rows = repository.add_many([entry(1_000)], "x", "regex")
    repository.soft_delete(rows[0].uuid)

    assert repository.unsynced() == []


def test_clear_sheet_row_sends_the_row_back_to_the_append_queue(repository):
    rows = repository.add_many([entry(1_000)], "x", "regex")
    repository.mark_synced(rows[0].uuid, 3)

    repository.clear_sheet_row(rows[0].uuid)

    pending = repository.unsynced()
    assert [r.uuid for r in pending] == [rows[0].uuid]
    assert pending[0].sheet_row is None
