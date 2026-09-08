"""The owner guard -- anyone can find the bot, only the owner may use it."""

from types import SimpleNamespace

from app.bot.handlers import OwnerOnlyMiddleware

OWNER = 12345


async def run_guard(user_id):
    calls = []

    async def handler(event, data):
        calls.append(event)
        return "handled"

    middleware = OwnerOnlyMiddleware(OWNER)
    user = None if user_id is None else SimpleNamespace(id=user_id)
    result = await middleware(handler, SimpleNamespace(), {"event_from_user": user})
    return result, calls


async def test_owner_passes_through():
    result, calls = await run_guard(OWNER)
    assert result == "handled"
    assert len(calls) == 1


async def test_stranger_is_dropped_before_the_handler_runs():
    result, calls = await run_guard(99999)
    assert result is None
    assert calls == []


async def test_missing_user_is_dropped():
    result, calls = await run_guard(None)
    assert result is None
    assert calls == []


async def test_owner_id_is_compared_as_an_int_not_a_string():
    result, calls = await run_guard(str(OWNER))
    assert result is None
    assert calls == []
