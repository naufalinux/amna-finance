"""The 21:00 daily recap.

Reads exclusively from SQLite (PRD stage 2) -- Sheets is an output mirror and
is never on this path, so the recap still fires when the Sheets API is down.
"""

from datetime import date, timedelta

import structlog

from app.bot import formatting

log = structlog.get_logger(__name__)


def build_recap_text(repository, day: date, currency: str = "IDR", title: str | None = None) -> str:
    entries = repository.entries_for_day(day)
    if not entries:
        return "No expenses logged today 🎉"
    totals = repository.totals_by_category(day, day)
    largest = max(entries, key=lambda e: e.amount)
    average = repository.daily_average(day, days=7)
    return formatting.recap(
        day, totals, largest=largest, average=average, currency=currency, title=title
    )


def build_period_text(repository, start: date, end: date, label: str, currency: str = "IDR") -> str:
    totals = repository.totals_by_category(start, end)
    return formatting.period_summary(label, totals, currency=currency)


def build_recap_job(bot, repository, settings):
    async def daily_recap_job() -> None:
        try:
            day = repository.today()
            text = build_recap_text(repository, day, currency=settings.default_currency)
            await bot.send_message(settings.telegram_owner_id, text)
            log.info("recap.sent", day=day.isoformat())
        except Exception as exc:
            log.error("recap.failed", error=str(exc))

    return daily_recap_job


def week_bounds(today: date) -> tuple[date, date]:
    """Monday..today of the current week."""
    return today - timedelta(days=today.weekday()), today


def month_bounds(today: date) -> tuple[date, date]:
    return today.replace(day=1), today
