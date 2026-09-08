"""Regex fast-path parser.

Most messages ("kopi 20k", "grab 45rb, parkir 5k") are trivially structured, so
they never need to reach an LLM. Anything this parser can't read confidently
falls through to `llm_parser`.
"""

import re

from app.parsing.categories import infer
from app.parsing.models import ParsedEntry, ParseResult

PARSER_NAME = "regex"

# Confidence tiers -- see module docstring in llm_parser for how these are used.
CONF_EXPLICIT = 0.95  # "45k", "45.000"  -- unit is unambiguous
CONF_BARE_LARGE = 0.9  # "45000"          -- almost certainly literal rupiah
CONF_BARE_SMALL = 0.6  # "45"             -- shorthand for 45.000? ask the user

MULTIPLIERS: dict[str, int] = {
    "k": 1_000,
    "rb": 1_000,
    "ribu": 1_000,
    "ribuan": 1_000,
    "jt": 1_000_000,
    "juta": 1_000_000,
    "jtan": 1_000_000,
    "jutaan": 1_000_000,
    "m": 1_000_000,
}

# A comma between two digits is a decimal or thousands separator ("2,5 juta",
# "250,000"), not an item boundary -- only split on the other commas.
_SPLIT_RE = re.compile(r"[;\n]+|,(?!\d)|(?<!\d),|\s+dan\s+|\s+\+\s+|\s{2,}")

_AMOUNT_RE = re.compile(
    r"(?:rp\.?\s*)?"
    r"(?P<num>\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)"
    r"\s*"
    r"(?P<suffix>ribuan|jutaan|ribu|juta|jtan|rb|jt|k|m)?"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)

# Words that carry no meaning for categorisation; dropped from the note.
_FILLER = {
    "rp", "idr", "rupiah", "beli", "bayar", "buat", "untuk", "di", "ke",
    "dari", "tadi", "barusan", "abis", "habis", "udah", "sudah", "aja",
    "sih", "nih", "gue", "gua", "saya", "aku", "seharga", "harga", "total",
}


def _parse_number(raw: str, suffix: str | None) -> tuple[int, float] | None:
    """Return (amount in minor units, confidence) or None."""
    text = raw.strip()
    separators = [c for c in text if c in ".,"]

    if separators:
        last = max(text.rfind("."), text.rfind(","))
        tail = text[last + 1 :]
        if len(tail) == 3 and not suffix:
            # 45.000 / 45,000 -- grouped thousands, already a literal amount.
            value = float(re.sub(r"[.,]", "", text))
            explicit = True
        elif len(tail) == 3 and suffix:
            # "1.500k" -- grouping plus a multiplier.
            value = float(re.sub(r"[.,]", "", text))
            explicit = True
        else:
            # 1.5jt -- decimal point.
            value = float(text.replace(",", "."))
            explicit = bool(suffix)
    else:
        value = float(text)
        explicit = False

    if suffix:
        value *= MULTIPLIERS[suffix.lower()]
        confidence = CONF_EXPLICIT
    elif explicit:
        confidence = CONF_EXPLICIT
    elif value >= 1000:
        confidence = CONF_BARE_LARGE
    else:
        # "makan 45" almost always means 45.000 in Indonesian shorthand, but
        # it is a guess -- flag it so the handler asks before writing.
        value *= 1000
        confidence = CONF_BARE_SMALL

    amount = int(round(value))
    return (amount, confidence) if amount > 0 else None


def _clean_note(text: str) -> str:
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    kept = [w for w in words if w.lower().strip(".,!?") not in _FILLER]
    return " ".join(kept).strip(" .,-:").strip()


def parse_fragment(fragment: str, currency: str = "IDR") -> ParsedEntry | None:
    match = _AMOUNT_RE.search(fragment)
    if not match:
        return None
    parsed = _parse_number(match.group("num"), match.group("suffix"))
    if parsed is None:
        return None
    amount, confidence = parsed

    note = _clean_note(fragment[: match.start()] + " " + fragment[match.end() :])
    return ParsedEntry(
        amount=amount,
        currency=currency,
        category=infer(note),
        note=note,
        confidence=confidence,
    )


def parse(message: str, currency: str = "IDR") -> ParseResult:
    """Split a message into fragments and read an entry out of each."""
    entries: list[ParsedEntry] = []
    for fragment in _SPLIT_RE.split(message or ""):
        if not fragment.strip():
            continue
        entry = parse_fragment(fragment, currency=currency)
        if entry is not None:
            entries.append(entry)
    return ParseResult(entries=entries, parser=PARSER_NAME)
