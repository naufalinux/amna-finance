"""Entrypoint: build the config, open the DB, wire the bot and scheduler, run.

One process, one event loop, long-polling only -- no public URL and no TLS.
"""

import asyncio

import structlog
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.bot.handlers import build_router
from app.config import get_settings
from app.jobs.daily_recap import build_recap_job
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
    return scheduler


async def run() -> None:
    settings = get_settings()
    configure_logging()

    engine = init_db(settings.db_path)
    repository = Repository(engine, timezone=settings.timezone)
    sheets = build_sheets_client(settings)
    sync = SyncService(repository, sheets)
    llm_parser = LLMParser(settings)

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

    try:
        await dispatcher.start_polling(bot, handle_signals=False)
    except TelegramUnauthorizedError:
        log.error("startup.bad_token", reason="Telegram rejected TELEGRAM_BOT_TOKEN")
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("shutdown")


if __name__ == "__main__":
    main()
