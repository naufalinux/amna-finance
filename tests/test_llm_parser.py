"""The Qwen -> Gemini failover. No test touches the network."""

import json

import httpx
import pytest

from app.parsing.llm_parser import LLMParser


class FakeSettings:
    llm_enabled = True
    llm_primary_base_url = "https://primary.test/v1"
    llm_primary_model = "qwen-test"
    llm_primary_api_key = "primary-key"
    llm_fallback_base_url = "https://fallback.test/v1"
    llm_fallback_model = "gemini-test"
    llm_fallback_api_key = "fallback-key"
    llm_timeout_seconds = 5.0

    @property
    def llm_primary_enabled(self):
        return self.llm_enabled and bool(self.llm_primary_api_key)

    @property
    def llm_fallback_enabled(self):
        return self.llm_enabled and bool(self.llm_fallback_api_key)

    @property
    def any_llm_enabled(self):
        return self.llm_primary_enabled or self.llm_fallback_enabled


GOOD = '{"entries":[{"amount":45000,"currency":"IDR","category":"Transport","note":"grab","confidence":0.9}]}'
OTHER = '{"entries":[{"amount":10000,"currency":"IDR","category":"Food","note":"kopi","confidence":0.8}]}'


def parser_with(responses):
    """`responses` maps model name -> str to return, or an Exception to raise."""
    parser = LLMParser(FakeSettings())
    calls = []

    async def fake_call(base_url, model, api_key, message):
        calls.append(model)
        outcome = responses[model]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    parser._call = fake_call
    return parser, calls


async def test_primary_success_never_reaches_the_fallback():
    parser, calls = parser_with({"qwen-test": GOOD, "gemini-test": OTHER})
    result = await parser.parse("grab 45k")
    assert result.entries[0].amount == 45_000
    assert calls == ["qwen-test"]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("no route"),
        httpx.ReadTimeout("timed out"),
        httpx.HTTPStatusError(
            "429",
            request=httpx.Request("POST", "https://primary.test"),
            response=httpx.Response(429),
        ),
        httpx.HTTPStatusError(
            "503",
            request=httpx.Request("POST", "https://primary.test"),
            response=httpx.Response(503),
        ),
    ],
    ids=["connect-error", "timeout", "rate-limited", "server-error"],
)
async def test_transport_failures_fail_over_to_gemini(failure):
    parser, calls = parser_with({"qwen-test": failure, "gemini-test": OTHER})
    result = await parser.parse("kopi 10k")
    assert result.entries[0].amount == 10_000
    assert calls == ["qwen-test", "gemini-test"]


@pytest.mark.parametrize(
    "bad",
    ["not json at all", "", '{"entries": "nope"}', '{"entries":[{"amount":0}]}', "{}"],
    ids=["prose", "empty", "wrong-type", "invalid-amount", "no-entries-key"],
)
async def test_unparseable_json_also_fails_over(bad):
    parser, calls = parser_with({"qwen-test": bad, "gemini-test": OTHER})
    result = await parser.parse("kopi 10k")
    assert result is not None
    assert result.entries[0].amount == 10_000
    assert calls == ["qwen-test", "gemini-test"]


async def test_both_providers_failing_returns_none_so_the_caller_degrades():
    parser, calls = parser_with(
        {"qwen-test": httpx.ConnectError("x"), "gemini-test": "garbage"}
    )
    assert await parser.parse("kopi 10k") is None
    assert calls == ["qwen-test", "gemini-test"]


async def test_fenced_json_is_accepted():
    parser, _ = parser_with({"qwen-test": f"```json\n{GOOD}\n```", "gemini-test": OTHER})
    result = await parser.parse("grab 45k")
    assert result.entries[0].amount == 45_000


async def test_empty_entries_is_a_valid_answer_not_a_failure():
    parser, calls = parser_with({"qwen-test": '{"entries":[]}', "gemini-test": OTHER})
    result = await parser.parse("halo")
    assert result.entries == []
    assert calls == ["qwen-test"]


async def test_llm_category_is_snapped_onto_the_taxonomy():
    parser, _ = parser_with({"qwen-test": OTHER, "gemini-test": OTHER})
    result = await parser.parse("kopi 10k")
    assert result.entries[0].category == "Food & Drink"


def test_disabled_without_any_key():
    settings = FakeSettings()
    settings.llm_primary_api_key = ""
    settings.llm_fallback_api_key = ""
    assert LLMParser(settings).enabled is False


async def test_only_fallback_configured_still_works():
    settings = FakeSettings()
    settings.llm_primary_api_key = ""
    parser = LLMParser(settings)
    calls = []

    async def fake_call(base_url, model, api_key, message):
        calls.append(model)
        return GOOD

    parser._call = fake_call
    assert (await parser.parse("grab 45k")).entries[0].amount == 45_000
    assert calls == ["gemini-test"]


async def test_call_posts_openai_shaped_request_to_the_right_endpoint(monkeypatch):
    """Guards the one piece of _call that a mocked-out failover test can't see."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": GOOD}}]}
        )

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    parser = LLMParser(FakeSettings())
    result = await parser.parse("grab 45k")

    assert result.entries[0].amount == 45_000
    assert seen["url"] == "https://primary.test/v1/chat/completions"
    assert seen["auth"] == "Bearer primary-key"
    assert seen["body"]["model"] == "qwen-test"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["messages"][-1] == {"role": "user", "content": "grab 45k"}
