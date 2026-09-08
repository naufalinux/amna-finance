"""The highest-value suite: the regex fast-path handles most real messages."""

import pytest

from app.parsing import regex_parser
from tests.conftest import load_messages

CASES = load_messages()


@pytest.mark.parametrize("case", CASES, ids=[c["message"][:30] or "empty" for c in CASES])
def test_parses_expected_entries(case):
    result = regex_parser.parse(case["message"])
    assert [(e.amount, e.category) for e in result.entries] == [
        tuple(x) for x in case["entries"]
    ]


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if "min_confidence" in c],
    ids=[c["message"][:30] for c in CASES if "min_confidence" in c],
)
def test_confidence_tier(case):
    result = regex_parser.parse(case["message"])
    assert result.min_confidence == pytest.approx(case["min_confidence"])


@pytest.mark.parametrize(
    "text,expected",
    [
        ("kopi 20k", 20_000),
        ("kopi 20rb", 20_000),
        ("kopi 20 ribu", 20_000),
        ("kopi 20.000", 20_000),
        ("kopi 20,000", 20_000),
        ("kopi 20000", 20_000),
        ("kopi 20", 20_000),
        ("sewa 1.5jt", 1_500_000),
        ("sewa 1,5 juta", 1_500_000),
        ("sewa 1500k", 1_500_000),
        ("sewa 1.500k", 1_500_000),
        ("sewa Rp1.500.000", 1_500_000),
    ],
)
def test_amount_forms_all_reach_the_same_number(text, expected):
    entries = regex_parser.parse(text).entries
    assert len(entries) == 1
    assert entries[0].amount == expected
    assert isinstance(entries[0].amount, int)


def test_bare_small_number_is_flagged_for_confirmation():
    """`makan 45` means 45.000, but we are not certain enough to write silently."""
    result = regex_parser.parse("makan 45")
    assert result.entries[0].amount == 45_000
    assert result.min_confidence < 0.7


def test_explicit_suffix_is_confident_enough_to_write():
    assert regex_parser.parse("makan 45k").min_confidence >= 0.7


def test_note_strips_filler_but_keeps_the_subject():
    entry = regex_parser.parse("tadi beli kopi di starbucks 55k").entries[0]
    assert "kopi" in entry.note and "starbucks" in entry.note
    assert "beli" not in entry.note.split()


def test_message_without_an_amount_yields_nothing():
    assert regex_parser.parse("halo apa kabar").entries == []


def test_decimal_comma_is_not_an_item_separator():
    result = regex_parser.parse("hotel 2,5 juta")
    assert len(result.entries) == 1
    assert result.entries[0].amount == 2_500_000


# --- parse_amount, shared with /edit ----------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("50k", 50_000),
        ("50rb", 50_000),
        ("50 ribu", 50_000),
        ("50.000", 50_000),
        ("50,000", 50_000),
        ("1.5jt", 1_500_000),
        ("2,5 juta", 2_500_000),
        ("Rp45.000", 45_000),
        ("rp 45k", 45_000),
        ("45000", 45_000),
        (" 145k ", 145_000),
    ],
)
def test_parse_amount_reads_every_money_syntax_the_parser_knows(text, expected):
    amount, confidence = regex_parser.parse_amount(text)
    assert amount == expected
    assert 0 < confidence <= 1


@pytest.mark.parametrize("text", ["", "  ", "banana", "45k dan 20k", "kopi 20k", "-5k"])
def test_parse_amount_refuses_anything_that_is_not_just_an_amount(text):
    """`/edit` takes an amount, not a message -- ambiguity must be rejected."""
    assert regex_parser.parse_amount(text) is None


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["message"])
def test_parse_amount_agrees_with_the_corpus_on_single_item_messages(case):
    """Whatever the corpus says an amount means, /edit must mean the same."""
    entries = case["entries"]
    if len(entries) != 1:
        pytest.skip("multi-item message: not a bare amount")
    parsed = regex_parser.parse(case["message"]).entries
    if not parsed:
        pytest.skip("corpus case the fast-path deliberately leaves to the LLM")

    # Feed /edit exactly the amount text the parser found in the message.
    fragment = regex_parser._AMOUNT_RE.search(case["message"]).group(0)
    assert regex_parser.parse_amount(fragment)[0] == parsed[0].amount
