"""Backups: never keep an unverified copy, never let retention drift."""

import gzip
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.jobs.backup import (
    FILENAME,
    backup_day,
    build_backup_job,
    keep_set,
    prune,
    run_backup,
)
from app.parsing.models import ParsedEntry


def entry(amount: int) -> ParsedEntry:
    return ParsedEntry(amount=amount, category="Food & Drink", note="x", confidence=0.95)


@pytest.fixture
def populated(repository):
    repository.add_many([entry(45_000), entry(20_000)], "grab 45k, kopi 20k", "regex")
    return repository


def touch(directory, day: date) -> None:
    (directory / FILENAME.format(day=day.isoformat())).write_bytes(b"x")


# --- the round trip ----------------------------------------------------------


def test_a_backup_round_trips_back_into_a_readable_database(populated, tmp_path):
    backups = tmp_path / "backups"

    result = run_backup(populated.engine.url.database, backups, date(2026, 9, 8))

    assert result.ok
    assert result.path.name == "expenses-2026-09-08.db.gz"

    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(result.path.read_bytes()))
    connection = sqlite3.connect(restored)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 2
        assert connection.execute("SELECT SUM(amount) FROM expenses").fetchone()[0] == 65_000
    finally:
        connection.close()


def test_a_backup_runs_against_a_live_database(populated, tmp_path):
    """The writer is not stopped for a backup, so WAL data must be included."""
    result = run_backup(populated.engine.url.database, tmp_path, populated.today())
    populated.add_many([entry(1_000)], "after", "regex")  # still writable

    assert result.ok
    assert populated.stats().rows == 3


def test_the_staging_file_is_never_left_behind(populated, tmp_path):
    run_backup(populated.engine.url.database, tmp_path, date(2026, 9, 8))
    assert list(tmp_path.glob(".*.tmp")) == []


def test_a_missing_database_is_reported_not_raised(tmp_path):
    result = run_backup(tmp_path / "nope.db", tmp_path / "backups", date(2026, 9, 8))

    assert not result.ok
    assert result.error
    assert list((tmp_path / "backups").glob("*.gz")) == []


def test_a_corrupt_copy_is_rejected_and_never_kept(populated, tmp_path, monkeypatch):
    """An unverified backup is worse than none -- it produces false confidence."""
    monkeypatch.setattr("app.jobs.backup._integrity_ok", lambda _path: False)

    result = run_backup(populated.engine.url.database, tmp_path, date(2026, 9, 8))

    assert not result.ok
    assert "integrity_check" in result.error
    assert list(tmp_path.glob("*.gz")) == []


# --- retention ---------------------------------------------------------------


def test_backup_day_reads_the_date_out_of_the_filename():
    assert backup_day("expenses-2026-09-08.db.gz") == date(2026, 9, 8)
    assert backup_day("something-else.txt") is None


def test_retention_keeps_the_recent_dailies_and_one_per_month():
    days = [date(2026, 9, 8) - timedelta(days=n) for n in range(400)]

    keep = keep_set(days, keep_daily=14, keep_monthly=12)

    recent = {date(2026, 9, 8) - timedelta(days=n) for n in range(14)}
    assert recent <= keep
    # September's daily window already covers its first-of-month, so 12 monthly
    # slots add 11 further dates on top of the 14 dailies.
    assert len(keep) == 14 + 11
    assert date(2025, 10, 1) in keep  # a monthly anchor from a year back
    assert date(2026, 5, 17) not in keep  # a mid-month straggler


def test_retention_keeps_the_first_backup_of_a_month_even_if_it_is_not_the_1st():
    days = [date(2026, 3, 7), date(2026, 3, 20), date(2026, 4, 2)]

    keep = keep_set(days, keep_daily=0, keep_monthly=12)

    assert keep == {date(2026, 3, 7), date(2026, 4, 2)}


def test_pruning_a_400_day_archive_leaves_exactly_the_retained_set(tmp_path):
    days = [date(2026, 9, 8) - timedelta(days=n) for n in range(400)]
    for day in days:
        touch(tmp_path, day)

    removed = prune(tmp_path, keep_daily=14, keep_monthly=12)

    survivors = {backup_day(p.name) for p in tmp_path.glob("*.gz")}
    assert survivors == keep_set(days, 14, 12)
    assert removed == 400 - len(survivors)


def test_pruning_ignores_files_that_are_not_backups(tmp_path):
    touch(tmp_path, date(2020, 1, 5))
    (tmp_path / "notes.txt").write_text("keep me")

    prune(tmp_path, keep_daily=0, keep_monthly=0)

    assert (tmp_path / "notes.txt").exists()
    assert list(tmp_path.glob("*.gz")) == []


def test_a_backup_prunes_as_it_goes(populated, tmp_path):
    touch(tmp_path, date(2020, 1, 1))

    result = run_backup(
        populated.engine.url.database,
        tmp_path,
        date(2026, 9, 8),
        keep_daily=1,
        keep_monthly=0,
    )

    assert result.pruned == 1
    assert [p.name for p in tmp_path.glob("*.gz")] == ["expenses-2026-09-08.db.gz"]


# --- the scheduled job -------------------------------------------------------


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def job_settings(tmp_path, db_path):
    return SimpleNamespace(
        db_path=db_path,
        backup_path=tmp_path / "backups",
        backup_keep_daily=14,
        backup_keep_monthly=12,
        owner_ids={7, 9},
    )


async def test_a_successful_backup_is_silent_and_shows_up_in_stats(populated, tmp_path):
    bot = FakeBot()
    settings = job_settings(tmp_path, populated.engine.url.database)

    await build_backup_job(bot, populated, settings)()

    assert bot.sent == []  # successes never interrupt anyone
    assert populated.stats().last_backup == populated.today().isoformat()


async def test_a_failed_backup_alerts_every_owner(populated, tmp_path, monkeypatch):
    """The one operational event worth interrupting the owner for."""
    monkeypatch.setattr("app.jobs.backup._integrity_ok", lambda _path: False)
    bot = FakeBot()
    settings = job_settings(tmp_path, populated.engine.url.database)

    await build_backup_job(bot, populated, settings)()

    assert {chat_id for chat_id, _ in bot.sent} == {7, 9}
    assert "Backup failed" in bot.sent[0][1]
    assert populated.stats().last_backup is None


async def test_an_unreachable_owner_does_not_break_the_job(populated, tmp_path, monkeypatch):
    monkeypatch.setattr("app.jobs.backup._integrity_ok", lambda _path: False)

    class BrokenBot(FakeBot):
        async def send_message(self, chat_id, text):
            raise RuntimeError("blocked the bot")

    settings = job_settings(tmp_path, populated.engine.url.database)
    await build_backup_job(BrokenBot(), populated, settings)()  # must not raise
