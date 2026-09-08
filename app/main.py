"""Entrypoint: build the config, open the DB, wire the bot and scheduler, run.

One process, one event loop, long-polling only -- no public URL and no TLS.

Shutdown is explicit rather than left to aiogram: systemd sends SIGTERM on
`stop`, `restart` and reboot, and the process must stop polling, get whatever
is still unsynced into the Sheet, and checkpoint the WAL before it exits.
`TimeoutStopSec` in the unit is only the backstop for that.
"""

import asyncio
import signal

import structlog
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.bot.handlers import build_router
from app.config import get_settings
from app.jobs.backup import build_backup_job
from app.jobs.daily_recap import build_recap_job
from app.jobs.reconcile import build_reconcile_job
from app.jobs.sync_retry import SyncService, build_sync_job
from app.logging_setup import configure_logging
from app.parsing.llm_parser import LLMParser
from app.storage.db import init_db
from app.storage.repository import Repository
from app.storage.sheets import build_sheets_client

log = structlog.get_logger(__name__)


def build_scheduler(bot, repository, sync, settings) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    hour, minute = settings.recap_hour_minute
    scheduler.add_job(
        build_recap_job(bot, repository, settings),
        CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
        id="daily_recap",
        replace_existing=True,
    )
    if sync.sheets.enabled:
        scheduler.add_job(
            build_sync_job(sync),
            IntervalTrigger(minutes=settings.sync_interval_minutes),
            id="sync_retry",
            replace_existing=True,
        )
        scheduler.add_job(
            build_reconcile_job(bot, repository, sync.sheets, settings),
            CronTrigger(
                day_of_week=settings.reconcile_weekday,
                hour=6,
                timezone=settings.timezone,
            ),
            id="reconcile",
            replace_existing=True,
        )
    if settings.backups_enabled:
        backup_hour, backup_minute = settings.backup_hour_minute
        scheduler.add_job(
            build_backup_job(bot, repository, settings),
            CronTrigger(
                hour=backup_hour, minute=backup_minute, timezone=settings.timezone
            ),
            id="backup",
            replace_existing=True,
        )
    return scheduler


def install_signal_handlers(dispatcher) -> None:
    """Turn SIGTERM/SIGINT into an orderly stop instead of an abrupt one."""
    loop = asyncio.get_running_loop()

    def request_stop(name: str) -> None:
        log.info("shutdown.requested", signal=name)
        # Returns once the current update finishes; the flush happens after
        # start_polling returns, in `run`'s finally block.
        asyncio.create_task(dispatcher.stop_polling())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop, sig.name)
        except NotImplementedError:  # pragma: no cover -- non-POSIX
            log.warning("shutdown.signals_unavailable", signal=sig.name)


async def shutdown(sync, engine) -> None:
    """Stop cleanly: nothing unsynced, nothing left in the WAL."""
    try:
        flushed = await asyncio.wait_for(sync.flush(), timeout=20)
        log.info("shutdown.flushed", rows=flushed)
    except Exception as exc:
        log.warning("shutdown.flush_failed", error=str(exc))
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        engine.dispose()
        log.info("shutdown.checkpointed")
    except Exception as exc:
        log.warning("shutdown.checkpoint_failed", error=str(exc))


async def run() -> None:
    settings = get_settings()
    configure_logging()

    engine = init_db(settings.db_path)
    repository = Repository(engine, timezone=settings.timezone)
    sheets = build_sheets_client(settings)
    sync = SyncService(repository, sheets)
    llm_parser = LLMParser(settings)

    if settings.sheets_enabled:
        try:
            await sheets.ensure_status_column()
        except Exception as exc:
            # A missing status column costs us the delete marker, not a write.
            log.warning("sheets.status_column_failed", error=str(exc))
    if not settings.backups_enabled:
        log.warning("backup.disabled", reason="BACKUP_PATH not set; no backups")

    if not llm_parser.enabled:
        log.warning("llm.disabled", reason="no LLM API key set; regex-only parsing")
    elif not settings.llm_fallback_enabled:
        log.warning("llm.no_fallback", reason="LLM_FALLBACK_API_KEY not set")

    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(repository, sync, llm_parser, settings))

    scheduler = build_scheduler(bot, repository, sync, settings)
    scheduler.start()

    hour, minute = settings.recap_hour_minute
    log.info(
        "startup",
        db=str(settings.db_path),
        timezone=settings.timezone,
        recap=f"{hour:02d}:{minute:02d}",
        sheets=sheets.enabled,
        llm=llm_parser.enabled,
    )

    install_signal_handlers(dispatcher)

    try:
        await dispatcher.start_polling(bot, handle_signals=False)
    except TelegramUnauthorizedError:
        log.error("startup.bad_token", reason="Telegram rejected TELEGRAM_BOT_TOKEN")
    finally:
        scheduler.shutdown(wait=False)
        await shutdown(sync, engine)
        await bot.session.close()
        log.info("shutdown.complete")


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("shutdown")


if __name__ == "__main__":
    main()
