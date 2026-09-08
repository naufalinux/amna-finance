"""Sheets sync: the immediate post-write push and the periodic retry sweep.

Both paths go through `SyncService.push`, so the retry job and the fire-and-
forget push after a message share one code path and one idempotency guarantee.
"""

import asyncio

import structlog

log = structlog.get_logger(__name__)


class SyncService:
    def __init__(self, repository, sheets):
        self.repository = repository
        self.sheets = sheets
        self._lock = asyncio.Lock()

    async def push(self, expenses) -> int:
        """Mirror `expenses` to Sheets. Returns how many were marked synced.

        Failures are swallowed and logged: the rows stay `sheet_synced = 0`
        and the retry sweep will try again.
        """
        if not self.sheets.enabled or not expenses:
            return 0
        async with self._lock:
            try:
                placed = await self.sheets.append(expenses)
            except Exception as exc:
                log.warning("sheets.append_failed", count=len(expenses), error=str(exc))
                return 0
            for expense in expenses:
                if expense.uuid in placed:
                    self.repository.mark_synced(expense.uuid, placed[expense.uuid] or None)
            log.info("sheets.appended", count=len(placed))
            return len(placed)

    async def flush(self) -> int:
        """Retry every unsynced row. Returns the number newly synced."""
        pending = self.repository.unsynced()
        if not pending:
            return 0
        log.info("sheets.flush", pending=len(pending))
        return await self.push(pending)

    def push_in_background(self, expenses) -> None:
        """Fire-and-forget, so the user is never waiting on Google."""
        if not self.sheets.enabled:
            return
        task = asyncio.create_task(self.push(expenses))
        # Keep a reference until done, otherwise the task can be GC'd mid-flight.
        self._background = getattr(self, "_background", set())
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def build_sync_job(sync: SyncService):
    async def sync_retry_job() -> None:
        try:
            await sync.flush()
        except Exception as exc:
            log.error("sync_retry.failed", error=str(exc))

    return sync_retry_job
