"""Reconciliation: detect divergence, and never repair it automatically."""

from types import SimpleNamespace

import pytest

from app.jobs.reconcile import compare, format_report, reconcile
from app.jobs.sync_retry import SyncService
from app.parsing.models import ParsedEntry
from tests.test_sync import FakeSheets


def entry(amount: int, category: str = "Food & Drink") -> ParsedEntry:
    return ParsedEntry(amount=amount, category=category, note="x", confidence=0.95)


@pytest.fixture
async def mirrored(repository):
    """A repository and a fake sheet that agree, ready to be broken."""
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    rows = repository.add_many([entry(45_000), entry(20_000)], "x", "regex")
    await sync.push(rows)
    return SimpleNamespace(repository=repository, sheets=sheets, sync=sync, rows=rows)


async def test_a_matching_sheet_reconciles_clean(mirrored):
    report = await reconcile(mirrored.repository, mirrored.sheets)

    assert report.clean
    assert report.checked == 2
    assert "matches the database" in format_report(report)


async def test_a_hand_deleted_sheet_row_is_reported_as_missing(mirrored):
    del mirrored.sheets.rows[0]

    report = await reconcile(mirrored.repository, mirrored.sheets)

    assert report.missing == [mirrored.rows[0].uuid]
    assert not report.clean
    assert "Missing from the Sheet" in format_report(report)


async def test_a_hand_added_sheet_row_is_reported_as_orphaned(mirrored):
    mirrored.sheets.rows.append(
        ["typed-by-hand", "2026-09-08", 5_000, "IDR", "Food & Drink", "", "", "", "active"]
    )

    report = await reconcile(mirrored.repository, mirrored.sheets)

    assert report.orphaned == ["typed-by-hand"]
    assert "In the Sheet only" in format_report(report)


@pytest.mark.parametrize("column,value", [(2, 999), (4, "Transport"), (8, "deleted")])
async def test_a_disagreeing_cell_is_reported_as_divergent(mirrored, column, value):
    mirrored.sheets.rows[0][column] = value

    report = await reconcile(mirrored.repository, mirrored.sheets)

    assert report.divergent == [mirrored.rows[0].uuid]
    assert "Values disagree" in format_report(report)


async def test_a_row_still_queued_for_sync_is_not_called_missing(repository):
    """It hasn't been written yet -- that's the sync job's business, not ours."""
    repository.add_many([entry(1_000)], "x", "regex")

    report = await reconcile(repository, FakeSheets())

    assert report.clean


async def test_an_undone_entry_must_read_as_deleted_in_the_sheet(mirrored):
    mirrored.repository.soft_delete_batch(mirrored.rows[0].batch_id)

    # Before the sweep the sheet still says active, and that is real divergence.
    before = await reconcile(mirrored.repository, mirrored.sheets)
    assert len(before.divergent) == 2

    await mirrored.sync.flush()
    after = await reconcile(mirrored.repository, mirrored.sheets)
    assert after.clean


async def test_reconciliation_never_changes_anything(mirrored):
    """PRD 8.3: an automatic repair against a human-edited sheet loses data."""
    mirrored.sheets.rows[0][2] = 999
    snapshot = [list(r) for r in mirrored.sheets.rows]

    await reconcile(mirrored.repository, mirrored.sheets)

    assert mirrored.sheets.rows == snapshot
    assert mirrored.repository.entries_for_day()[0].amount == 45_000


def test_compare_counts_every_kind_of_divergence_at_once():
    rows = [
        SimpleNamespace(uuid="a", amount=1, category="Food & Drink", status="active", sheet_synced=1),
        SimpleNamespace(uuid="b", amount=2, category="Food & Drink", status="active", sheet_synced=1),
    ]
    sheet = [
        ["a", "", "999", "IDR", "Food & Drink", "", "", "", "active"],  # divergent
        ["c", "", "3", "IDR", "Food & Drink", "", "", "", "active"],  # orphan
    ]  # "b" is missing entirely

    report = compare(rows, sheet)

    assert report.divergent == ["a"]
    assert report.missing == ["b"]
    assert report.orphaned == ["c"]
    assert report.checked == 2


def test_a_long_report_is_truncated_for_telegram():
    rows = [
        SimpleNamespace(
            uuid=f"u{n}", amount=1, category="Food & Drink", status="active", sheet_synced=1
        )
        for n in range(9)
    ]

    text = format_report(compare(rows, []))

    assert "Missing from the Sheet: 9" in text
    assert "and 4 more" in text
