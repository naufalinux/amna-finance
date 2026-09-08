"""LLM fallback parser.

Primary is Qwen3 on OpenRouter's free tier; fallback is Gemini Flash Lite.
Both speak the OpenAI-compatible /chat/completions API, so one client covers
each -- only base URL, model and key differ.

The fallback fires on anything that leaves us without a usable answer: network
errors, timeouts, HTTP 429/5xx, and responses whose JSON does not validate. If
both providers fail we return None and the caller degrades to the regex result
(PRD 7.4 -- a message is never dropped).
"""

import json
import re

import httpx
import structlog
from pydantic import ValidationError

from app.parsing.categories import CATEGORIES, normalize
from app.parsing.models import ParsedEntry, ParseResult

log = structlog.get_logger(__name__)

PARSER_NAME = "llm"

_CATEGORY_LIST = ", ".join(CATEGORIES)

SYSTEM_PROMPT = f"""You extract expenses from short, informal Indonesian or \
English chat messages and return strict JSON.

Return ONLY a JSON object of this exact shape:
{{"entries":[{{"amount":45000,"currency":"IDR","category":"Transport",\
"note":"grab","confidence":0.95}}]}}

Rules:
- amount is an INTEGER in rupiah minor units. No decimals, no separators, no
  currency symbol. "45k"/"45rb"/"45 ribu" -> 45000. "1.5jt" -> 1500000.
- A bare number under 1000 is Indonesian shorthand for thousands:
  "makan 45" -> 45000.
- One object per distinct item. "lunch 45k, kopi 20k" is two entries.
- category MUST be one of: {_CATEGORY_LIST}. Use "Uncategorized" if unsure.
- note is a short human label for the item, in the user's own words.
- confidence is 0..1, your honest certainty about amount AND category.
- If the message contains no expense at all, return {{"entries":[]}}.
- Output the JSON object and nothing else. No prose, no markdown fence."""

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMParser:
    def __init__(self, settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.any_llm_enabled

    def _providers(self) -> list[tuple[str, str, str, str]]:
        """(label, base_url, model, api_key) in the order we try them."""
        out = []
        s = self.settings
        if s.llm_primary_enabled:
            out.append(
                ("primary", s.llm_primary_base_url, s.llm_primary_model, s.llm_primary_api_key)
            )
        if s.llm_fallback_enabled:
            out.append(
                ("fallback", s.llm_fallback_base_url, s.llm_fallback_model, s.llm_fallback_api_key)
            )
        return out

    async def parse(self, message: str) -> ParseResult | None:
        for label, base_url, model, api_key in self._providers():
            try:
                content = await self._call(base_url, model, api_key, message)
            except Exception as exc:  # network, timeout, HTTP status
                log.warning(
                    "llm.provider_failed", provider=label, model=model, error=str(exc)
                )
                continue
            result = self._to_result(content)
            if result is None:
                log.warning(
                    "llm.invalid_json", provider=label, model=model, snippet=content[:200]
                )
                continue
            log.info(
                "llm.parsed", provider=label, model=model, entries=len(result.entries)
            )
            return result
        return None

    async def _call(self, base_url: str, model: str, api_key: str, message: str) -> str:
        payload = {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        url = base_url.rstrip("/") + "/chat/completions"
        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
        return body["choices"][0]["message"]["content"] or ""

    @staticmethod
    def _to_result(content: str) -> ParseResult | None:
        """Validate the model's JSON. Returns None so the caller can fail over."""
        text = content.strip()
        fenced = _FENCE_RE.search(text)
        if fenced:
            text = fenced.group(1)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            return None
        entries: list[ParsedEntry] = []
        for item in data["entries"]:
            if not isinstance(item, dict):
                return None
            try:
                entry = ParsedEntry(**item)
            except (ValidationError, TypeError, ValueError):
                return None
            entry.category = normalize(entry.category)
            entries.append(entry)
        return ParseResult(entries=entries, parser=PARSER_NAME)
