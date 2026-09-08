"""Weekly Sheet-vs-database reconciliation.

Backups answer "can I get the data back?". This answers the other question:
"does the mirror still say what the record says?" -- the one thing a correction
feature can quietly get wrong.

It **reports, never repairs.** An automatic repair against a human-edited
spreadsheet is how you lose data: we cannot tell a hand-typed correction from
an accidental overwrite, so the owner decides.
"""

from dataclasses import dataclass, field

import structlog

from app.storage.sheets import HEADER

log = structlog.get_logger(__name__)

_UUID = HEADER.index("uuid")
_AMOUNT = HEADER.index("amount")
_CATEGORY = HEADER.index("category")
_STATUS = HEADER.index("status")


@dataclass
class ReconcileReport:
    missing: list[str] = field(default_factory=list)  # synced here, absent there
    orphaned: list[str] = field(default_factory=list)  # there, unknown here
    divergent: list[str] = field(default_factory=list)  # both, disagreeing
    checked: int = 0

    @property
    def clean(self) -> bool:
        return not (self.missing or self.orphaned or self.divergent)


def _cell(row: list, index: int) -> str:
    """One cell as text. The API hands back strings; a fake or a numeric cell
    may hand back an int, so normalise rather than trust the type."""
    return str(row[index]).strip() if index < len(row) else ""


def compare(expenses, sheet_rows) -> ReconcileReport:
    """Compare every synced row against the sheet, in both directions."""
    report = ReconcileReport(checked=len(expenses))
    by_uuid = {_cell(row, _UUID): row for row in sheet_rows if _cell(row, _UUID)}

    for expense in expenses:
        row = by_uuid.pop(expense.uuid, None)
        if row is None:
            # Only a row we believe we wrote is genuinely missing; anything
            # still queued is the sync job's business, not ours.
            if expense.sheet_synced:
                report.missing.append(expense.uuid)
            continue
        if (
            _cell(row, _AMOUNT) != str(expense.amount)
            or _cell(row, _CATEGORY) != expense.category
            or _cell(row, _STATUS) != expense.status
        ):
            report.divergent.append(expense.uuid)

    # Whatever is left in the sheet has no counterpart here: hand-added, or
    # orphaned when a manual reorder made us re-append a row.
    report.orphaned.extend(by_uuid)
    return report


def format_report(report: ReconcileReport) -> str:
    if report.clean:
        return f"✅ Sheet matches the database ({report.checked} rows checked)."

    lines = [f"⚠️ Sheet and database disagree ({report.checked} rows checked)", ""]
    for label, uuids in (
        ("Missing from the Sheet", report.missing),
        ("In the Sheet only", report.orphaned),
        ("Values disagree", report.divergent),
    ):
        if uuids:
            lines.append(f"{label}: {len(uuids)}")
            lines += [f"  {u}" for u in uuids[:5]]
            if len(uuids) > 5:
                lines.append(f"  … and {len(uuids) - 5} more")
    lines += ["", "Nothing was changed. Fix by hand, or re-log the entry."]
    return "\n".join(lines)


async def reconcile(repository, sheets) -> ReconcileReport:
    """Fetch both sides and compare. Raises if the Sheets API is unreachable."""
    sheet_rows = await sheets.fetch_rows()
    expenses = repository.all_rows()
    report = compare(expenses, sheet_rows)
    log.info(
        "reconcile.done",
        checked=report.checked,
        missing=len(report.missing),
        orphaned=len(report.orphaned),
        divergent=len(report.divergent),
    )
    return report


def build_reconcile_job(bot, repository, sheets, settings):
    async def reconcile_job() -> None:
        try:
            report = await reconcile(repository, sheets)
        except Exception as exc:
            log.error("reconcile.failed", error=str(exc))
            return
        if report.clean:
            return  # silence is the good outcome
        text = format_report(report)
        for owner_id in settings.owner_ids:
            try:
                await bot.send_message(owner_id, text)
            except Exception as exc:
                log.error("reconcile.send_failed", owner_id=owner_id, error=str(exc))

    return reconcile_job
