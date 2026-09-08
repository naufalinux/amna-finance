"""The correction commands: /last, /undo, /edit, /cat and the shared picker.

Handlers are pulled straight off the router and called with lightweight
doubles, so these tests exercise the real closures without going near
Telegram. Nothing here touches the network.
"""

from types import SimpleNamespace

import pytest

from app.bot.handlers import build_router
from app.jobs.sync_retry import SyncService
from app.parsing.models import ParsedEntry
from tests.test_sync import FakeSheets

ME = 7
PARTNER = 9


class FakeMessage:
    """Just enough of aiogram's Message for a handler to reply to."""

    def __init__(self, user_id: int = ME, text: str = ""):
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.replies: list[str] = []
        self.keyboards: list = []

    async def answer(self, text, reply_markup=None, **_kwargs):
        self.replies.append(text)
        self.keyboards.append(reply_markup)
        return self

    @property
    def reply(self) -> str:
        return self.replies[-1]

    @property
    def keyboard(self):
        return self.keyboards[-1]


class FakeCallback:
    def __init__(self, data: str, user_id: int = ME):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage(user_id)
        self.message.edit_text = self._edit  # type: ignore[method-assign]
        self.answers: list[str | None] = []

    async def _edit(self, text, **_kwargs):
        self.message.replies.append(text)

    async def answer(self, text=None, **_kwargs):
        self.answers.append(text)


@pytest.fixture
def bot(repository):
    """The router's handlers, addressable by name, plus the fake sheet."""
    sheets = FakeSheets()
    sync = SyncService(repository, sheets)
    settings = SimpleNamespace(
        owner_ids={ME, PARTNER},
        default_currency="IDR",
        confidence_threshold=0.7,
    )
    router = build_router(
        repository, sync, SimpleNamespace(enabled=False), settings
    )
    handlers = {
        h.callback.__name__: h.callback
        for observer in (router.message, router.callback_query)
        for h in observer.handlers
    }
    return SimpleNamespace(
        handlers=handlers, repository=repository, sync=sync, sheets=sheets
    )


def entry(amount: int, category: str = "Food & Drink", note: str = "x") -> ParsedEntry:
    return ParsedEntry(amount=amount, category=category, note=note, confidence=0.95)


def command(args: str | None):
    return SimpleNamespace(args=args)


async def log(bot, *entries, user_id: int = ME, raw: str = "x", day=None):
    rows = bot.repository.add_many(
        list(entries), raw, "regex", occurred_on=day, user_id=user_id
    )
    await bot.sync.push(rows)
    return rows


# --- /last -------------------------------------------------------------------


async def test_last_shows_nothing_when_there_are_no_entries(bot):
    message = FakeMessage()
    await bot.handlers["on_last"](message)
    assert message.reply == "Nothing logged yet."


async def test_last_shows_every_item_of_the_newest_message(bot):
    await log(bot, entry(45_000, "Transport", "grab"), entry(20_000, note="kopi"))
    message = FakeMessage()

    await bot.handlers["on_last"](message)

    assert "grab" in message.reply and "kopi" in message.reply
    assert "Rp65.000" in message.reply


async def test_last_changes_nothing(bot):
    rows = await log(bot, entry(1_000))
    await bot.handlers["on_last"](FakeMessage())
    assert bot.repository.get(rows[0].uuid) is not None


# --- /undo -------------------------------------------------------------------


async def test_undo_with_no_entries_is_not_an_error(bot):
    message = FakeMessage()
    await bot.handlers["on_undo"](message)
    assert message.reply == "Nothing to undo."


async def test_undo_removes_the_whole_message_not_just_the_last_row(bot):
    """PRD 3.3: three items logged in one message are undone as a unit."""
    await log(bot, entry(45_000), entry(20_000), entry(5_000))
    message = FakeMessage()

    await bot.handlers["on_undo"](message)

    assert "3 items" in message.reply and "Rp70.000" in message.reply
    assert bot.repository.entries_for_day() == []


async def test_undo_twice_removes_the_two_most_recent_messages(bot):
    first = await log(bot, entry(1_000), raw="first")
    await log(bot, entry(2_000), raw="second")
    await log(bot, entry(3_000), raw="third")

    for _ in range(2):
        await bot.handlers["on_undo"](FakeMessage())

    remaining = bot.repository.entries_for_day()
    assert [r.uuid for r in remaining] == [first[0].uuid]


async def test_undo_never_touches_the_other_owners_entry(bot):
    """PRD 3.5: my partner logs an expense; my /undo does not remove it."""
    mine = await log(bot, entry(1_000), user_id=ME, raw="mine")
    theirs = await log(bot, entry(2_000), user_id=PARTNER, raw="theirs")

    await bot.handlers["on_undo"](FakeMessage(user_id=ME))

    assert bot.repository.get(mine[0].uuid) is None
    assert bot.repository.get(theirs[0].uuid) is not None


async def test_undo_propagates_to_the_sheet_as_a_status_change(bot):
    rows = await log(bot, entry(1_000))
    await bot.handlers["on_undo"](FakeMessage())
    await bot.sync.flush()

    assert len(bot.sheets.rows) == 1  # the row is mutated, never removed
    assert bot.sheets.cell(rows[0].uuid, 8) == "deleted"


async def test_undoing_an_older_entry_asks_first(bot):
    from datetime import timedelta

    yesterday = bot.repository.today() - timedelta(days=1)
    rows = await log(bot, entry(65_000), day=yesterday)
    message = FakeMessage()

    await bot.handlers["on_undo"](message)

    assert "remove" in message.reply.lower()
    assert message.keyboard is not None
    assert bot.repository.get(rows[0].uuid) is not None  # nothing removed yet


async def test_confirming_the_older_undo_removes_it(bot):
    from datetime import timedelta

    yesterday = bot.repository.today() - timedelta(days=1)
    rows = await log(bot, entry(65_000), day=yesterday)
    message = FakeMessage()
    await bot.handlers["on_undo"](message)

    token = message.keyboard.inline_keyboard[0][0].callback_data.split(":")[1]
    callback = FakeCallback(f"fix:{token}:yes")
    await bot.handlers["on_fix"](callback)

    assert "Removed 1 item" in callback.message.reply
    assert bot.repository.get(rows[0].uuid) is None


async def test_cancelling_the_older_undo_keeps_it(bot):
    from datetime import timedelta

    yesterday = bot.repository.today() - timedelta(days=1)
    rows = await log(bot, entry(65_000), day=yesterday)
    message = FakeMessage()
    await bot.handlers["on_undo"](message)

    token = message.keyboard.inline_keyboard[0][0].callback_data.split(":")[1]
    await bot.handlers["on_fix"](FakeCallback(f"fix:{token}:no"))

    assert bot.repository.get(rows[0].uuid) is not None


# --- /edit -------------------------------------------------------------------


@pytest.mark.parametrize(
    "argument,expected",
    [("145k", 145_000), ("145rb", 145_000), ("145.000", 145_000), ("1.5jt", 1_500_000)],
)
async def test_edit_accepts_every_amount_syntax_the_parser_knows(bot, argument, expected):
    rows = await log(bot, entry(45_000, note="grab"))
    message = FakeMessage()

    await bot.handlers["on_edit"](message, command(argument))

    assert bot.repository.get(rows[0].uuid).amount == expected


async def test_edit_reports_the_before_and_after(bot):
    """PRD 3.1: I sent `grab 45k` but it was 145k."""
    await log(bot, entry(45_000, "Transport", "grab"))
    message = FakeMessage()

    await bot.handlers["on_edit"](message, command("145k"))

    assert "Rp45.000" in message.reply and "Rp145.000" in message.reply


async def test_edit_rejects_an_unreadable_amount(bot):
    rows = await log(bot, entry(45_000))
    message = FakeMessage()

    await bot.handlers["on_edit"](message, command("banana"))

    assert "Couldn't read that amount" in message.reply
    assert bot.repository.get(rows[0].uuid).amount == 45_000


async def test_edit_with_no_argument_is_rejected(bot):
    message = FakeMessage()
    await bot.handlers["on_edit"](message, command(None))
    assert "Couldn't read that amount" in message.reply


async def test_edit_with_nothing_logged_says_so(bot):
    message = FakeMessage()
    await bot.handlers["on_edit"](message, command("45k"))
    assert message.reply == "Nothing to edit."


async def test_edit_updates_the_sheet_row_in_place(bot):
    rows = await log(bot, entry(45_000))
    await bot.handlers["on_edit"](FakeMessage(), command("145k"))
    await bot.sync.flush()

    assert len(bot.sheets.rows) == 1
    assert bot.sheets.cell(rows[0].uuid, 2) == 145_000


async def test_edit_on_an_unsynced_row_appends_once_already_correct(bot):
    """PRD 7.6: the row never reached the sheet, so it appends exactly once."""
    rows = bot.repository.add_many([entry(45_000)], "grab 45k", "regex", user_id=ME)

    await bot.handlers["on_edit"](FakeMessage(), command("145k"))
    await bot.sync.flush()

    assert len(bot.sheets.rows) == 1
    assert bot.sheets.cell(rows[0].uuid, 2) == 145_000


# --- /cat --------------------------------------------------------------------


async def test_cat_snaps_the_argument_onto_the_taxonomy(bot):
    """PRD 3.2: `titip 15k` landed in Uncategorized; /cat groceries fixes it."""
    rows = await log(bot, entry(15_000, "Uncategorized", "titip"))
    message = FakeMessage()

    await bot.handlers["on_cat"](message, command("groceries"))

    assert bot.repository.get(rows[0].uuid).category == "Groceries"
    assert "Uncategorized" in message.reply and "Groceries" in message.reply


@pytest.mark.parametrize("argument", ["food", "Food & Drink", "makan"])
async def test_cat_accepts_the_ways_a_category_gets_typed(bot, argument):
    rows = await log(bot, entry(15_000, "Uncategorized"))

    await bot.handlers["on_cat"](FakeMessage(), command(argument))

    assert bot.repository.get(rows[0].uuid).category == "Food & Drink"


async def test_cat_rejects_an_unknown_category_and_lists_the_valid_ones(bot):
    rows = await log(bot, entry(15_000, "Uncategorized"))
    message = FakeMessage()

    await bot.handlers["on_cat"](message, command("cryptocurrency"))

    assert "don't know that category" in message.reply
    assert "Groceries" in message.reply and "Transport" in message.reply
    assert bot.repository.get(rows[0].uuid).category == "Uncategorized"


async def test_cat_with_no_argument_is_rejected(bot):
    message = FakeMessage()
    await bot.handlers["on_cat"](message, command(None))
    assert "don't know that category" in message.reply


async def test_cat_updates_the_sheet_row_in_place(bot):
    rows = await log(bot, entry(15_000, "Uncategorized"))
    await bot.handlers["on_cat"](FakeMessage(), command("groceries"))
    await bot.sync.flush()

    assert len(bot.sheets.rows) == 1
    assert bot.sheets.cell(rows[0].uuid, 4) == "Groceries"


# --- the shared multi-entry picker ------------------------------------------


async def test_editing_a_multi_item_message_asks_which_one(bot):
    rows = await log(bot, entry(45_000, note="grab"), entry(20_000, note="kopi"))
    message = FakeMessage()

    await bot.handlers["on_edit"](message, command("145k"))

    assert "Which one?" in message.reply
    assert len(message.keyboard.inline_keyboard) == 3  # two items plus cancel
    assert [r.amount for r in bot.repository.entries_for_day()] == [45_000, 20_000]


async def test_picking_an_item_corrects_only_that_item(bot):
    rows = await log(bot, entry(45_000, note="grab"), entry(20_000, note="kopi"))
    message = FakeMessage()
    await bot.handlers["on_edit"](message, command("145k"))

    token = message.keyboard.inline_keyboard[0][0].callback_data.split(":")[1]
    await bot.handlers["on_fix"](FakeCallback(f"fix:{token}:1"))  # the kopi

    assert bot.repository.get(rows[0].uuid).amount == 45_000
    assert bot.repository.get(rows[1].uuid).amount == 145_000


async def test_the_picker_is_shared_by_cat(bot):
    rows = await log(bot, entry(45_000, note="grab"), entry(20_000, note="kopi"))
    message = FakeMessage()
    await bot.handlers["on_cat"](message, command("groceries"))

    token = message.keyboard.inline_keyboard[0][0].callback_data.split(":")[1]
    await bot.handlers["on_fix"](FakeCallback(f"fix:{token}:0"))

    assert bot.repository.get(rows[0].uuid).category == "Groceries"
    assert bot.repository.get(rows[1].uuid).category == "Food & Drink"


async def test_cancelling_the_picker_changes_nothing(bot):
    rows = await log(bot, entry(45_000), entry(20_000))
    message = FakeMessage()
    await bot.handlers["on_edit"](message, command("145k"))

    token = message.keyboard.inline_keyboard[0][0].callback_data.split(":")[1]
    callback = FakeCallback(f"fix:{token}:no")
    await bot.handlers["on_fix"](callback)

    assert "Nothing changed" in callback.message.reply
    assert [r.amount for r in bot.repository.entries_for_day()] == [45_000, 20_000]


async def test_an_expired_token_is_reported_not_applied(bot):
    """Tokens live in memory, so a restart or a stale keyboard just expires."""
    await log(bot, entry(45_000))
    callback = FakeCallback("fix:deadbeef:0")

    await bot.handlers["on_fix"](callback)

    assert callback.answers == ["That prompt expired."]
    assert bot.repository.entries_for_day()[0].amount == 45_000


async def test_a_token_cannot_be_replayed(bot):
    rows = await log(bot, entry(45_000), entry(20_000))
    message = FakeMessage()
    await bot.handlers["on_edit"](message, command("145k"))
    token = message.keyboard.inline_keyboard[0][0].callback_data.split(":")[1]

    await bot.handlers["on_fix"](FakeCallback(f"fix:{token}:0"))
    replay = FakeCallback(f"fix:{token}:1")
    await bot.handlers["on_fix"](replay)

    assert replay.answers == ["That prompt expired."]
    assert bot.repository.get(rows[1].uuid).amount == 20_000


async def test_picking_an_item_that_was_deleted_meanwhile_is_handled(bot):
    rows = await log(bot, entry(45_000), entry(20_000))
    message = FakeMessage()
    await bot.handlers["on_edit"](message, command("145k"))
    token = message.keyboard.inline_keyboard[0][0].callback_data.split(":")[1]

    bot.repository.soft_delete(rows[0].uuid)
    callback = FakeCallback(f"fix:{token}:0")
    await bot.handlers["on_fix"](callback)

    assert callback.message.reply == "That entry is gone."


# --- attribution -------------------------------------------------------------


async def test_a_logged_message_records_who_logged_it(bot):
    message = FakeMessage(user_id=ME, text="grab 45k")
    await bot.handlers["on_text"](message)

    rows = bot.repository.entries_for_day()
    assert [r.user_id for r in rows] == [ME]
