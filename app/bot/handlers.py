"""Telegram handlers: owner guard, expense logging, and read-only commands."""

import uuid as uuid_lib
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
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
from app.jobs.reconcile import format_report, reconcile
from app.parsing import categories, regex_parser
from app.parsing.models import ParsedEntry

log = structlog.get_logger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    """Drop every update that did not come from the owner.

    This is the single most important guard in the project: the bot token is
    public-ish and anyone who finds the bot can message it.
    """

    def __init__(self, owner_ids: set[int]):
        self.owner_ids = owner_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in self.owner_ids:
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
/last — show what /undo would remove
/undo — remove your last message
/edit <amount> — fix the amount
/cat <category> — fix the category
/sync — force a Google Sheets flush
/reconcile — compare the Sheet against the database
/stats — database and sync status"""


def build_router(repository, sync, llm_parser, settings) -> Router:
    router = Router(name="expenses")
    router.message.outer_middleware(OwnerOnlyMiddleware(settings.owner_ids))
    router.callback_query.outer_middleware(OwnerOnlyMiddleware(settings.owner_ids))

    currency = settings.default_currency
    # Entries awaiting an inline-keyboard yes/no, keyed by a short token.
    pending: dict[str, tuple[list[ParsedEntry], str, str, int | None]] = {}
    # Corrections awaiting a target, same short-token scheme. Both dicts live
    # only in memory: a token expires with the process, which is fine because
    # the user simply re-sends the command.
    corrections: dict[str, dict] = {}

    def _token() -> str:
        return uuid_lib.uuid4().hex[:8]

    def _persist(entries, raw_message, parser_name, user_id):
        rows = repository.add_many(
            entries, raw_message, parser_name, user_id=user_id
        )
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

    @router.message(Command("reconcile"))
    async def on_reconcile(message: Message) -> None:
        if not sync.sheets.enabled:
            await message.answer("Google Sheets is not configured.")
            return
        await message.answer("Comparing the Sheet against the database…")
        try:
            report = await reconcile(repository, sync.sheets)
        except Exception as exc:
            await message.answer(f"Couldn't read the Sheet: {exc}")
            return
        await message.answer(format_report(report))

    @router.message(Command("stats"))
    async def on_stats(message: Message) -> None:
        await message.answer(formatting.stats(repository.stats()))

    # --- corrections --------------------------------------------------------

    def _picker(kind: str, value, entries) -> tuple[str, InlineKeyboardMarkup]:
        """One inline keyboard shared by /edit and /cat."""
        token = _token()
        corrections[token] = {
            "kind": kind,
            "value": value,
            "uuids": [e.uuid for e in entries],
        }
        buttons = [
            [
                InlineKeyboardButton(
                    text=formatting.picker_label(expense, currency),
                    callback_data=f"fix:{token}:{index}",
                )
            ]
            for index, expense in enumerate(entries)
        ]
        buttons.append(
            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"fix:{token}:no")]
        )
        return formatting.picker_prompt(entries, currency), InlineKeyboardMarkup(
            inline_keyboard=buttons
        )

    def _apply_amount(expense, amount: int) -> str:
        previous = expense.amount
        updated = repository.update_amount(expense.uuid, amount)
        if updated is None:
            return "That entry is gone."
        sync.push_in_background([updated])
        return formatting.amount_correction(updated, previous, currency)

    def _apply_category(expense, category: str) -> str:
        previous = expense.category
        updated = repository.update_category(expense.uuid, category)
        if updated is None:
            return "That entry is gone."
        sync.push_in_background([updated])
        return formatting.category_correction(updated, previous)

    def _do_undo(batch) -> str:
        removed = repository.soft_delete_batch(batch[0].batch_id)
        sync.push_in_background(removed)
        return formatting.undo_confirmation(removed, currency)

    @router.message(Command("last"))
    async def on_last(message: Message) -> None:
        batch = repository.last_batch(message.from_user.id)
        await message.answer(formatting.last_batch(batch, currency))

    @router.message(Command("undo"))
    async def on_undo(message: Message) -> None:
        batch = repository.last_batch(message.from_user.id)
        if not batch:
            await message.answer("Nothing to undo.")
            return

        # Removing something from an earlier day is much more likely to be a
        # mistake than removing today's last message, so ask first.
        if batch[0].occurred_at != repository.today().isoformat():
            token = _token()
            corrections[token] = {"kind": "undo", "batch_id": batch[0].batch_id}
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🗑 Remove", callback_data=f"fix:{token}:yes"
                        ),
                        InlineKeyboardButton(
                            text="❌ Keep", callback_data=f"fix:{token}:no"
                        ),
                    ]
                ]
            )
            await message.answer(
                formatting.undo_prompt(batch, currency), reply_markup=keyboard
            )
            return

        await message.answer(_do_undo(batch))

    @router.message(Command("edit"))
    async def on_edit(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        parsed = regex_parser.parse_amount(raw) if raw else None
        if parsed is None:
            await message.answer(
                "Couldn't read that amount. Try `/edit 45k`.", parse_mode="Markdown"
            )
            return
        amount, _ = parsed

        batch = repository.last_batch(message.from_user.id)
        if not batch:
            await message.answer("Nothing to edit.")
            return
        if len(batch) > 1:
            text, keyboard = _picker("amount", amount, batch)
            await message.answer(text, reply_markup=keyboard)
            return

        await message.answer(_apply_amount(batch[0], amount))

    @router.message(Command("cat"))
    async def on_cat(message: Message, command: CommandObject) -> None:
        raw = (command.args or "").strip()
        category = categories.normalize(raw) if raw else None
        if category is None or category not in categories.CATEGORIES:
            known = ", ".join(categories.CATEGORIES)
            await message.answer(f"I don't know that category. Try one of:\n\n{known}")
            return

        batch = repository.last_batch(message.from_user.id)
        if not batch:
            await message.answer("Nothing to edit.")
            return
        if len(batch) > 1:
            text, keyboard = _picker("category", category, batch)
            await message.answer(text, reply_markup=keyboard)
            return

        await message.answer(_apply_category(batch[0], category))

    @router.callback_query(F.data.startswith("fix:"))
    async def on_fix(callback: CallbackQuery) -> None:
        _, token, choice = callback.data.split(":", 2)
        staged = corrections.pop(token, None)
        if staged is None:
            await callback.answer("That prompt expired.")
            return
        if choice == "no":
            await callback.message.edit_text("❌ Nothing changed.")
            await callback.answer()
            return

        if staged["kind"] == "undo":
            batch = repository.entries_in_batch(staged["batch_id"])
            text = _do_undo(batch) if batch else "Nothing to undo."
        else:
            expense = repository.get(staged["uuids"][int(choice)])
            if expense is None:
                text = "That entry is gone."
            elif staged["kind"] == "amount":
                text = _apply_amount(expense, staged["value"])
            else:
                text = _apply_category(expense, staged["value"])

        await callback.message.edit_text(text)
        await callback.answer()

    # --- inline confirmation ------------------------------------------------

    @router.callback_query(F.data.startswith("log:"))
    async def on_confirm(callback: CallbackQuery) -> None:
        _, decision, token = callback.data.split(":", 2)
        staged = pending.pop(token, None)
        if staged is None:
            await callback.answer("That prompt expired.")
            return
        entries, raw_message, parser_name, user_id = staged
        if decision == "no":
            await callback.message.edit_text("❌ Discarded.")
            await callback.answer()
            return
        rows = _persist(entries, raw_message, parser_name, user_id)
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
            token = _token()
            pending[token] = (
                result.entries,
                raw,
                parser_name,
                message.from_user.id,
            )
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

        rows = _persist(result.entries, raw, parser_name, message.from_user.id)
        await message.answer(formatting.confirmation(rows, currency))

    return router
