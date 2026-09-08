"""Telegram handlers: owner guard, expense logging, and read-only commands."""

import uuid as uuid_lib
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from app.bot import formatting
from app.jobs.daily_recap import (
    build_period_text,
    build_recap_text,
    month_bounds,
    week_bounds,
)
from app.parsing import regex_parser
from app.parsing.models import ParsedEntry

log = structlog.get_logger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    """Drop every update that did not come from the owner.

    This is the single most important guard in the project: the bot token is
    public-ish and anyone who finds the bot can message it.
    """

    def __init__(self, owner_id: int):
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id != self.owner_id:
            log.warning(
                "auth.rejected", user_id=getattr(user, "id", None)
            )
            return None
        return await handler(event, data)


HELP_TEXT = """👋 Send me an expense and I'll log it.

  `grab 45k, kopi hitam 20rb`

Commands:
/today — today's recap
/week — this week so far
/month — this month so far
/sync — force a Google Sheets flush
/stats — database and sync status"""


def build_router(repository, sync, llm_parser, settings) -> Router:
    router = Router(name="expenses")
    router.message.outer_middleware(OwnerOnlyMiddleware(settings.telegram_owner_id))
    router.callback_query.outer_middleware(
        OwnerOnlyMiddleware(settings.telegram_owner_id)
    )

    currency = settings.default_currency
    # Entries awaiting an inline-keyboard yes/no, keyed by a short token.
    pending: dict[str, tuple[list[ParsedEntry], str, str]] = {}

    def _persist(entries, raw_message, parser_name):
        rows = repository.add_many(entries, raw_message, parser_name)
        sync.push_in_background(rows)
        return rows

    # --- commands ----------------------------------------------------------

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer(HELP_TEXT, parse_mode="Markdown")

    @router.message(Command("help"))
    async def on_help(message: Message) -> None:
        await message.answer(HELP_TEXT, parse_mode="Markdown")

    @router.message(Command("today"))
    async def on_today(message: Message) -> None:
        day = repository.today()
        await message.answer(build_recap_text(repository, day, currency=currency))

    @router.message(Command("week"))
    async def on_week(message: Message) -> None:
        start, end = week_bounds(repository.today())
        await message.answer(
            build_period_text(repository, start, end, "this week", currency=currency)
        )

    @router.message(Command("month"))
    async def on_month(message: Message) -> None:
        start, end = month_bounds(repository.today())
        await message.answer(
            build_period_text(repository, start, end, "this month", currency=currency)
        )

    @router.message(Command("sync"))
    async def on_sync(message: Message) -> None:
        if not sync.sheets.enabled:
            await message.answer("Google Sheets is not configured.")
            return
        synced = await sync.flush()
        remaining = repository.stats().unsynced
        await message.answer(f"Synced {synced} row(s). {remaining} still pending.")

    @router.message(Command("stats"))
    async def on_stats(message: Message) -> None:
        await message.answer(formatting.stats(repository.stats()))

    # --- inline confirmation ------------------------------------------------

    @router.callback_query(F.data.startswith("log:"))
    async def on_confirm(callback: CallbackQuery) -> None:
        _, decision, token = callback.data.split(":", 2)
        staged = pending.pop(token, None)
        if staged is None:
            await callback.answer("That prompt expired.")
            return
        entries, raw_message, parser_name = staged
        if decision == "no":
            await callback.message.edit_text("❌ Discarded.")
            await callback.answer()
            return
        rows = _persist(entries, raw_message, parser_name)
        await callback.message.edit_text(formatting.confirmation(rows, currency))
        await callback.answer()

    # --- the main path ------------------------------------------------------

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        raw = (message.text or "").strip()
        result = regex_parser.parse(raw, currency=currency)
        parser_name = regex_parser.PARSER_NAME

        # Escalate to the LLM when regex found nothing, or found something it
        # isn't sure about.
        if llm_parser.enabled and (
            not result.ok or result.min_confidence < settings.confidence_threshold
        ):
            llm_result = await llm_parser.parse(raw)
            if llm_result is not None and llm_result.ok:
                result = llm_result
                parser_name = llm_result.parser

        if not result.ok:
            await message.answer(
                "I couldn't find an amount in that. Try `kopi 20k`.",
                parse_mode="Markdown",
            )
            return

        if result.min_confidence < settings.confidence_threshold:
            token = uuid_lib.uuid4().hex[:8]
            pending[token] = (result.entries, raw, parser_name)
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Log it", callback_data=f"log:yes:{token}"),
                        InlineKeyboardButton(text="❌ Cancel", callback_data=f"log:no:{token}"),
                    ]
                ]
            )
            await message.answer(
                formatting.confirm_prompt(result.entries, currency), reply_markup=keyboard
            )
            return

        rows = _persist(result.entries, raw, parser_name)
        await message.answer(formatting.confirmation(rows, currency))

    return router
