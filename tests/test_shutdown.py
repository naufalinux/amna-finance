"""Graceful shutdown: systemd's SIGTERM must not cost us an unsynced row."""

import asyncio
import signal

from app.jobs.sync_retry import SyncService
from app.main import install_signal_handlers, shutdown
from app.parsing.models import ParsedEntry
from tests.test_sync import FakeSheets


def entry(amount: int) -> ParsedEntry:
    return ParsedEntry(amount=amount, category="Food & Drink", note="x", confidence=0.95)


class FakeDispatcher:
    def __init__(self):
        self.stopped = False

    async def stop_polling(self):
        self.stopped = True


async def test_shutdown_flushes_what_was_still_unsynced(repository):
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    repository.add_many([entry(45_000), entry(20_000)], "x", "regex")
    assert len(repository.unsynced()) == 2

    await shutdown(sync, repository.engine)

    assert len(sheets.rows) == 2
    assert repository.unsynced() == []


async def test_shutdown_leaves_nothing_in_the_wal_to_replay(repository, tmp_path):
    repository.add_many([entry(1_000)], "x", "regex")
    wal = tmp_path / "test.db-wal"
    assert wal.exists() and wal.stat().st_size > 0

    await shutdown(SyncService(repository, FakeSheets()), repository.engine)

    # Checkpointed, then the last connection closed -- SQLite removes the file.
    assert not wal.exists() or wal.stat().st_size == 0


async def test_a_sheets_outage_does_not_block_the_exit(repository):
    """We stop cleanly either way; the rows stay dirty for the next start."""
    sync = SyncService(repository, FakeSheets(fail_times=99))
    repository.add_many([entry(1_000)], "x", "regex")

    await shutdown(sync, repository.engine)  # must not raise or hang

    assert len(repository.unsynced()) == 1


async def test_shutdown_survives_an_already_closed_engine(repository):
    sync = SyncService(repository, FakeSheets())
    repository.engine.dispose()

    await shutdown(sync, repository.engine)  # must not raise


async def test_sigterm_stops_polling(repository):
    dispatcher = FakeDispatcher()
    install_signal_handlers(dispatcher)

    signal.raise_signal(signal.SIGTERM)
    await asyncio.sleep(0.05)  # let the loop deliver the signal and run the task

    assert dispatcher.stopped
