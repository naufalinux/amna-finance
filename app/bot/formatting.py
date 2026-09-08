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


def entry_label(expense) -> str:
    """How one entry is named in a correction message."""
    return expense.note or expense.raw_message


def last_batch(expenses, currency: str = "IDR") -> str:
    """What /undo and /edit would act on. Read-only, so it is safe to guess."""
    if not expenses:
        return "Nothing logged yet."
    total = sum(e.amount for e in expenses)
    day = date.fromisoformat(expenses[0].occurred_at)
    lines = [f"🧾 Last entry — {pretty_date(day)}", ""]
    for expense in expenses:
        lines.append(
            f"• {expense.category:<16} {money(expense.amount, currency)}"
            f"  {entry_label(expense)}"
        )
    if len(expenses) > 1:
        lines += ["", f"Total {money(total, currency)}"]
    lines += ["", "/undo removes it · /edit <amount> · /cat <category>"]
    return "\n".join(lines)


def undo_confirmation(expenses, currency: str = "IDR") -> str:
    total = sum(e.amount for e in expenses)
    count = len(expenses)
    noun = "item" if count == 1 else "items"
    return f"🗑 Removed {count} {noun} — −{money(total, currency)}"


def undo_prompt(expenses, currency: str = "IDR") -> str:
    """Asked before undoing something that is not from today."""
    day = date.fromisoformat(expenses[0].occurred_at)
    total = sum(e.amount for e in expenses)
    count = len(expenses)
    noun = "item" if count == 1 else "items"
    return (
        f"That's from {day.day} {_MONTH_NAMES[day.month - 1]}"
        f" — remove {count} {noun}, {money(total, currency)}?"
    )


def amount_correction(expense, previous: int, currency: str = "IDR") -> str:
    return (
        f"✏️ {entry_label(expense)}: "
        f"{money(previous, currency)} → {money(expense.amount, currency)}"
    )


def category_correction(expense, previous: str) -> str:
    return f"🏷 {entry_label(expense)}: {previous} → {expense.category}"


def picker_prompt(expenses, currency: str = "IDR") -> str:
    return "Which one?\n\n" + "\n".join(
        f"• {entry_label(e)}  {money(e.amount, currency)}" for e in expenses
    )


def picker_label(expense, currency: str = "IDR") -> str:
    """Button text for the multi-entry picker; Telegram caps this at 64 chars."""
    label = f"{entry_label(expense)} {money(expense.amount, currency)}"
    return label if len(label) <= 60 else label[:57] + "..."


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
        f"Rows:        {snapshot.rows}",
        f"Unsynced:    {snapshot.unsynced}",
        f"Last sync:   {snapshot.last_sync or 'never'}",
        f"Last backup: {snapshot.last_backup or 'never'}",
    ]
    if snapshot.parser_counts:
        lines += ["", "By parser:"]
        for name, count in sorted(
            snapshot.parser_counts.items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"  {name:<8} {count}")
    return "\n".join(lines)
