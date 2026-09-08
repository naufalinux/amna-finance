"""Nightly verified backup of the SQLite system of record.

Two decisions shape this module:

- It uses the stdlib `sqlite3.Connection.backup()` API rather than shelling out
  to the `sqlite3` binary. That call is online-safe against a live WAL writer,
  and it needs no external executable inside the hardened systemd unit.
- A backup is verified with `PRAGMA integrity_check` *before* it is kept. An
  unverified backup is worse than no backup, because it produces false
  confidence. A corrupt copy is deleted and the owners are told.

Successes are silent and visible in `/stats`; a failure is the one operational
event worth interrupting someone for.
"""

import gzip
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

FILENAME = "expenses-{day}.db.gz"
_GLOB = "expenses-*.db.gz"


@dataclass(frozen=True)
class BackupResult:
    path: Path | None
    error: str | None = None
    pruned: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


def _snapshot(db_path: Path, destination: Path) -> None:
    """Copy a live database, WAL and all, without blocking the writer."""
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _integrity_ok(path: Path) -> bool:
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    return bool(result) and result[0] == "ok"


def backup_day(name: str) -> date | None:
    """The date encoded in a backup filename, or None if it isn't one."""
    stem = name.removeprefix("expenses-").removesuffix(".db.gz")
    try:
        return date.fromisoformat(stem)
    except ValueError:
        return None


def keep_set(days: list[date], keep_daily: int, keep_monthly: int) -> set[date]:
    """Which backup dates survive: recent dailies plus one per month.

    Every backup from the last `keep_daily` days, plus the first backup of each
    of the last `keep_monthly` months. At a few MB each this is trivially small.
    """
    ordered = sorted(days, reverse=True)
    keep = set(ordered[:keep_daily])

    firsts: dict[tuple[int, int], date] = {}
    for day in sorted(days):
        firsts.setdefault((day.year, day.month), day)
    for month in sorted(firsts, reverse=True)[:keep_monthly]:
        keep.add(firsts[month])
    return keep


def prune(directory: Path, keep_daily: int, keep_monthly: int) -> int:
    """Delete the backups retention no longer covers. Returns how many went."""
    dated = {
        path: backup_day(path.name)
        for path in directory.glob(_GLOB)
    }
    dated = {path: day for path, day in dated.items() if day is not None}
    keep = keep_set(list(dated.values()), keep_daily, keep_monthly)

    removed = 0
    for path, day in dated.items():
        if day not in keep:
            path.unlink()
            removed += 1
    return removed


def run_backup(
    db_path: Path,
    backup_dir: Path,
    day: date,
    keep_daily: int = 14,
    keep_monthly: int = 12,
) -> BackupResult:
    """Snapshot, verify, compress, prune. Never keeps an unverified copy."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    staged = backup_dir / f".expenses-{day.isoformat()}.db.tmp"
    final = backup_dir / FILENAME.format(day=day.isoformat())

    try:
        _snapshot(db_path, staged)
        if not _integrity_ok(staged):
            staged.unlink(missing_ok=True)
            log.error("backup.corrupt", day=day.isoformat())
            return BackupResult(None, error="integrity_check failed on the copy")

        with staged.open("rb") as raw, gzip.open(final, "wb") as compressed:
            shutil.copyfileobj(raw, compressed)
    except Exception as exc:
        log.error("backup.failed", day=day.isoformat(), error=str(exc))
        final.unlink(missing_ok=True)
        return BackupResult(None, error=str(exc))
    finally:
        staged.unlink(missing_ok=True)

    pruned = prune(backup_dir, keep_daily, keep_monthly)
    log.info(
        "backup.done",
        path=str(final),
        bytes=final.stat().st_size,
        pruned=pruned,
    )
    return BackupResult(final, pruned=pruned)


def build_backup_job(bot, repository, settings):
    async def backup_job() -> None:
        day = repository.today()
        result = run_backup(
            settings.db_path,
            settings.backup_path,
            day,
            keep_daily=settings.backup_keep_daily,
            keep_monthly=settings.backup_keep_monthly,
        )
        if result.ok:
            repository.set_state("last_backup", day.isoformat())
            return

        text = (
            f"🚨 Backup failed for {day.isoformat()}.\n\n{result.error}\n\n"
            "The database itself is fine, but there is no verified copy from "
            "tonight. Check the disk and the journal."
        )
        for owner_id in settings.owner_ids:
            try:
                await bot.send_message(owner_id, text)
            except Exception as exc:
                log.error("backup.alert_failed", owner_id=owner_id, error=str(exc))

    return backup_job
