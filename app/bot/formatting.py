"""Telegram message templates.

Amounts are integers in minor units everywhere; formatting to "Rp45.000"
happens here and nowhere else.
"""

from datetime import date

_DAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]
_MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def money(amount: int, currency: str = "IDR") -> str:
    grouped = f"{int(amount):,}".replace(",", ".")
    return f"Rp{grouped}" if currency == "IDR" else f"{currency} {grouped}"


def pretty_date(day: date) -> str:
    return f"{_DAY_NAMES[day.weekday()]}, {day.day} {_MONTH_NAMES[day.month - 1]} {day.year}"


def confirmation(expenses, currency: str = "IDR") -> str:
    total = sum(e.amount for e in expenses)
    count = len(expenses)
    noun = "item" if count == 1 else "items"
    lines = [f"✅ Logged {count} {noun} — {money(total, currency)}", ""]
    for expense in expenses:
        note = f"  {expense.note}" if expense.note else ""
        lines.append(f"• {expense.category:<16} {money(expense.amount, currency)}{note}")
    return "\n".join(lines)


def confirm_prompt(entries, currency: str = "IDR") -> str:
    total = sum(e.amount for e in entries)
    lines = ["🤔 Not sure I read this right — log it?", ""]
    for entry in entries:
        note = f"  {entry.note}" if entry.note else ""
        lines.append(f"• {entry.category:<16} {money(entry.amount, currency)}{note}")
    if len(entries) > 1:
        lines += ["", f"Total {money(total, currency)}"]
    return "\n".join(lines)


def recap(
    day: date,
    entries,
    average: float = 0.0,
    currency: str = "IDR",
    title: str | None = None,
) -> str:
    """Daily recap: every item logged today, in the order it was entered."""
    if not entries:
        return "No expenses logged today 🎉"
    total = sum(e.amount for e in entries)

    heading = title or pretty_date(day)
    lines = [f"📊 {heading}", ""]
    names = [entry.note or entry.raw_message for entry in entries]
    width = max(len(name) for name in names)
    for entry, name in zip(entries, names):
        lines.append(f"• {name:<{width}}  {money(entry.amount, currency)}")

    lines += ["", f"Total: {money(total, currency)}"]

    if average > 0:
        delta = (total - average) / average * 100
        direction = "above" if delta >= 0 else "below"
        lines.append(f"{abs(delta):.0f}% {direction} your 7-day average.")

    return "\n".join(lines)


def period_summary(label: str, totals, currency: str = "IDR") -> str:
    if not totals:
        return f"No expenses logged {label} 🎉"
    total = sum(t.total for t in totals)
    lines = [f"📊 {label.capitalize()} — {money(total, currency)}", ""]
    width = max(len(t.category) for t in totals)
    for item in totals:
        lines.append(
            f"  {item.category:<{width}}  {money(item.total, currency):>12}  ({item.count})"
        )
    return "\n".join(lines)


def stats(snapshot) -> str:
    lines = [
        "📦 Stats",
        "",
        f"Rows:      {snapshot.rows}",
        f"Unsynced:  {snapshot.unsynced}",
        f"Last sync: {snapshot.last_sync or 'never'}",
    ]
    if snapshot.parser_counts:
        lines += ["", "By parser:"]
        for name, count in sorted(
            snapshot.parser_counts.items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"  {name:<8} {count}")
    return "\n".join(lines)
