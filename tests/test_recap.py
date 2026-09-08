from datetime import date, timedelta

from app.bot import formatting
from app.jobs.daily_recap import (
    build_period_text,
    build_recap_text,
    month_bounds,
    week_bounds,
)
from app.parsing.models import ParsedEntry


def entry(amount, category, note="") -> ParsedEntry:
    return ParsedEntry(amount=amount, category=category, note=note, confidence=0.95)


def test_recap_on_an_empty_day(repository):
    assert build_recap_text(repository, repository.today()) == "No expenses logged today 🎉"


def test_recap_reports_total_breakdown_and_largest(repository):
    today = repository.today()
    # Mirrors the PRD 7.2 example: Food leads on total, but the single biggest
    # line item is the one Groceries run.
    repository.add_many(
        [
            entry(30_000, "Food & Drink", "makan siang"),
            entry(30_000, "Food & Drink", "makan malam"),
            entry(22_000, "Food & Drink", "kopi"),
            entry(15_000, "Food & Drink", "snack"),
            entry(15_000, "Food & Drink", "es teh"),
            entry(45_000, "Transport", "grab"),
            entry(25_000, "Transport", "gojek"),
            entry(15_000, "Transport", "parkir"),
            entry(50_000, "Groceries", "Superindo"),
        ],
        "x",
        "regex",
        occurred_on=today,
    )
    text = build_recap_text(repository, today)

    assert "Rp247.000" in text
    assert "Food & Drink" in text and "Rp112.000" in text
    assert "(5)" in text and "(3)" in text
    assert "Largest: Rp50.000 — Superindo" in text


def test_recap_compares_against_the_seven_day_average(repository):
    today = repository.today()
    for offset in range(1, 8):
        repository.add_many([entry(100_000, "Food & Drink")], "x", "regex",
                            occurred_on=today - timedelta(days=offset))
    repository.add_many([entry(121_000, "Food & Drink")], "x", "regex", occurred_on=today)

    assert "21% above your 7-day average." in build_recap_text(repository, today)


def test_recap_says_below_when_spending_is_down(repository):
    today = repository.today()
    for offset in range(1, 8):
        repository.add_many([entry(100_000, "Food & Drink")], "x", "regex",
                            occurred_on=today - timedelta(days=offset))
    repository.add_many([entry(50_000, "Food & Drink")], "x", "regex", occurred_on=today)

    assert "50% below your 7-day average." in build_recap_text(repository, today)


def test_recap_ignores_other_days(repository):
    today = repository.today()
    repository.add_many([entry(10_000, "Food & Drink")], "x", "regex", occurred_on=today)
    repository.add_many([entry(999_000, "Food & Drink")], "x", "regex",
                        occurred_on=today - timedelta(days=1))
    assert "Rp10.000" in build_recap_text(repository, today)
    assert "Rp999.000" not in build_recap_text(repository, today)


def test_period_summary_aggregates_the_whole_range(repository):
    today = repository.today()
    for offset in range(3):
        repository.add_many([entry(10_000, "Transport")], "x", "regex",
                            occurred_on=today - timedelta(days=offset))
    text = build_period_text(repository, today - timedelta(days=2), today, "this week")
    assert "Rp30.000" in text
    assert "Transport" in text and "(3)" in text


def test_period_summary_when_empty(repository):
    today = repository.today()
    assert build_period_text(repository, today, today, "this week") == (
        "No expenses logged this week 🎉"
    )


def test_week_bounds_start_on_monday():
    wednesday = date(2026, 9, 9)
    start, end = week_bounds(wednesday)
    assert start == date(2026, 9, 7)  # Monday
    assert end == wednesday


def test_month_bounds_start_on_the_first():
    start, end = month_bounds(date(2026, 9, 9))
    assert start == date(2026, 9, 1)
    assert end == date(2026, 9, 9)


def test_money_formatting_uses_indonesian_grouping():
    assert formatting.money(0) == "Rp0"
    assert formatting.money(5_000) == "Rp5.000"
    assert formatting.money(1_500_000) == "Rp1.500.000"
    assert formatting.money(1_000, "USD") == "USD 1.000"


def test_confirmation_pluralises_and_totals():
    class Row:
        def __init__(self, amount, category, note):
            self.amount, self.category, self.note = amount, category, note

    one = formatting.confirmation([Row(45_000, "Transport", "grab")])
    assert "Logged 1 item — Rp45.000" in one

    two = formatting.confirmation(
        [Row(45_000, "Transport", "grab"), Row(20_000, "Food & Drink", "kopi")]
    )
    assert "Logged 2 items — Rp65.000" in two
