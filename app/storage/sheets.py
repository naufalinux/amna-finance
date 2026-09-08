"""Google Sheets mirror.

Sheets is never the system of record -- it is an asynchronous, human-readable
copy of what is already committed to SQLite. Every write is idempotent via the
uuid in column A, so a retry after an ambiguous failure cannot duplicate a row.

When credentials are absent, `build_sheets_client` returns a no-op client so no
caller needs to branch on whether the mirror is configured.
"""

import asyncio

import structlog

log = structlog.get_logger(__name__)

HEADER = ["uuid", "date", "amount", "currency", "category", "note", "raw", "parser"]
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


class NullSheetsClient:
    """Stand-in used when Sheets is not configured."""

    enabled = False

    async def append(self, expenses):  # noqa: ARG002
        return {}

    async def health(self) -> str:
        return "disabled"


class SheetsClient:
    enabled = True

    def __init__(self, credentials_path, sheet_id: str, worksheet: str):
        self.credentials_path = credentials_path
        self.sheet_id = sheet_id
        self.worksheet_name = worksheet
        self._worksheet = None

    # --- blocking internals, always called via asyncio.to_thread ------------

    def _get_worksheet(self):
        if self._worksheet is not None:
            return self._worksheet
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(
            str(self.credentials_path), scopes=SCOPES
        )
        spreadsheet = gspread.authorize(creds).open_by_key(self.sheet_id)
        try:
            worksheet = spreadsheet.worksheet(self.worksheet_name)
        except Exception:
            worksheet = spreadsheet.add_worksheet(
                title=self.worksheet_name, rows=1000, cols=len(HEADER)
            )
            worksheet.append_row(HEADER, value_input_option="RAW")
        if not worksheet.row_values(1):
            worksheet.append_row(HEADER, value_input_option="RAW")
        self._worksheet = worksheet
        return worksheet

    def _append_blocking(self, expenses) -> dict[str, int]:
        worksheet = self._get_worksheet()
        existing = set(worksheet.col_values(1))
        pending = [e for e in expenses if e.uuid not in existing]
        already = {e.uuid: 0 for e in expenses if e.uuid in existing}
        if not pending:
            return already

        first_row = len(worksheet.col_values(1)) + 1
        worksheet.append_rows(
            [e.sheet_values() for e in pending], value_input_option="RAW"
        )
        placed = {e.uuid: first_row + i for i, e in enumerate(pending)}
        return {**already, **placed}

    # --- async surface ------------------------------------------------------

    async def append(self, expenses) -> dict[str, int]:
        """Append rows not already present. Returns {uuid: sheet_row}.

        Raises on API failure -- the caller leaves the rows unsynced and the
        retry job picks them up.
        """
        if not expenses:
            return {}
        return await asyncio.to_thread(self._append_blocking, list(expenses))

    async def health(self) -> str:
        try:
            await asyncio.to_thread(self._get_worksheet)
        except Exception as exc:
            return f"error: {exc}"
        return "ok"


def build_sheets_client(settings):
    if not settings.sheets_enabled:
        log.warning(
            "sheets.disabled",
            reason="GOOGLE_SHEET_ID or GOOGLE_CREDENTIALS_PATH not set",
        )
        return NullSheetsClient()
    return SheetsClient(
        settings.google_credentials_path, settings.google_sheet_id, settings.sheet_worksheet
    )
