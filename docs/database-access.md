# Accessing the SQLite database from a GUI client (DBeaver)

SQLite is the system of record (see the [README's Data model section](../README.md#️-data-model)) — the Google Sheet is only a mirror, so anything you need to slice, audit, or double-check should come from `expenses.db` directly rather than the Sheet.

The database file lives at `DB_PATH` (default `data/expenses.db`, see `.env` / `.env.example`). It runs in **WAL mode**, which means three files travel together and must **all** be present when you copy the database anywhere:

```
expenses.db          the database itself
expenses.db-wal      write-ahead log — recent writes not yet checkpointed into the main file
expenses.db-shm      shared-memory index for the WAL file
```

If you copy only `expenses.db`, you may be missing the most recent rows.

---

## 1. Local access (running the bot on your own machine)

This is the simple case — just point DBeaver at the file.

1. **Database Navigator → New Database Connection → SQLite.**
2. **Path:** browse to `data/expenses.db` in the repo (or wherever `DB_PATH` points).
3. First time only: DBeaver will prompt to download the SQLite JDBC driver — accept it.
4. **Finish.**

**While the bot is running**, open the connection **read-only**:
Edit Connection → General → check "Connection read-only". WAL mode allows
concurrent readers without blocking the bot's writer, but a GUI client that
issues a write (even an accidental cell edit) can collide with the bot's own
writes. Read-only removes that risk entirely.

---

## 2. Remote access (database lives on the VPS)

SQLite has no network protocol — there is no host/port to point DBeaver at.
Pick one of the two approaches below, depending on whether you want to browse
the **live** file or a **safe snapshot**.

### Option A — SSHFS mount (live, read-only recommended)

Mount the VPS data directory onto your local machine, then open the mounted
path in DBeaver exactly as in §1.

```bash
mkdir -p ~/mnt/amna-finance
sshfs your-user@your-vps:/opt/expense-agent/data ~/mnt/amna-finance
```

Point DBeaver at `~/mnt/amna-finance/expenses.db` and set the connection to
**read-only** (see §1) — this is not optional here, since you are now looking
at the bot's live file over a network filesystem.

Unmount when done: `umount ~/mnt/amna-finance` (or `fusermount -u` on Linux).

### Option B — Safe snapshot (recommended for anything beyond a quick look)

Ask SQLite itself to make a consistent, WAL-safe copy on the VPS, then pull
just that one file down. This is the same mechanism the backup job (P7) uses,
and it cannot be corrupted by a concurrent writer:

```bash
# on the VPS
sqlite3 /opt/expense-agent/data/expenses.db ".backup /tmp/expenses-snapshot.db"

# from your machine
scp your-user@your-vps:/tmp/expenses-snapshot.db ./expenses-snapshot.db
```

Open `expenses-snapshot.db` in DBeaver as a normal local file (§1). No `-wal`
or `-shm` sidecar files are needed — `.backup` folds everything into one
self-contained file. It's a point-in-time copy, so re-run it whenever you want
fresh data; there is nothing to keep in sync in the meantime.

**Prefer Option B by default.** It's one extra command, has zero risk of
disturbing the bot's writes, and doesn't depend on keeping an SSHFS mount
alive.

---

## 3. Essential daily queries

Money is stored as an **integer in whole rupiah** (`45000`, never `45000.0`
or cents) — no division needed when reading it back. Soft-deleted rows
(`deleted_at IS NOT NULL`) are excluded from every query below; that's what
`/undo` sets.

### Today's spend

```sql
SELECT SUM(amount) AS total_today, COUNT(*) AS items
FROM expenses
WHERE occurred_at = date('now', 'localtime')
  AND deleted_at IS NULL;
```

### Today's breakdown by category

```sql
SELECT category, SUM(amount) AS total, COUNT(*) AS n
FROM expenses
WHERE occurred_at = date('now', 'localtime')
  AND deleted_at IS NULL
GROUP BY category
ORDER BY total DESC;
```

### This week (Monday–today) by category

```sql
SELECT category, SUM(amount) AS total, COUNT(*) AS n
FROM expenses
WHERE occurred_at >= date('now', 'localtime', '-' || ((strftime('%w', 'now', 'localtime') + 6) % 7) || ' days')
  AND deleted_at IS NULL
GROUP BY category
ORDER BY total DESC;
```

### This month by category

```sql
SELECT category, SUM(amount) AS total, COUNT(*) AS n
FROM expenses
WHERE occurred_at >= date('now', 'localtime', 'start of month')
  AND deleted_at IS NULL
GROUP BY category
ORDER BY total DESC;
```

### Last 20 entries (most recent first)

```sql
SELECT occurred_at, amount, category, note, parser, confidence
FROM expenses
WHERE deleted_at IS NULL
ORDER BY id DESC
LIMIT 20;
```

### Largest single expense in the last 30 days

```sql
SELECT occurred_at, amount, category, note, raw_message
FROM expenses
WHERE occurred_at >= date('now', 'localtime', '-30 days')
  AND deleted_at IS NULL
ORDER BY amount DESC
LIMIT 1;
```

### 7-day daily average (for sanity-checking the recap's "% vs average" line)

```sql
SELECT occurred_at, SUM(amount) AS daily_total
FROM expenses
WHERE occurred_at >= date('now', 'localtime', '-7 days')
  AND occurred_at < date('now', 'localtime')
  AND deleted_at IS NULL
GROUP BY occurred_at
ORDER BY occurred_at;
```

### Rows waiting to sync to Google Sheets

```sql
SELECT id, occurred_at, amount, category, sheet_row
FROM expenses
WHERE sheet_synced = 0
  AND deleted_at IS NULL
ORDER BY id;
```

Non-empty for more than ~10 minutes usually means the Sheets API is down or
misconfigured — check `/sync` and `/stats` in Telegram first.

### Entries that need re-categorising

```sql
SELECT id, occurred_at, amount, note, raw_message
FROM expenses
WHERE category = 'Uncategorized'
  AND deleted_at IS NULL
ORDER BY id DESC;
```

### Low-confidence entries (worth a manual glance)

```sql
SELECT id, occurred_at, amount, category, note, confidence, parser
FROM expenses
WHERE confidence < 0.7
  AND deleted_at IS NULL
ORDER BY id DESC;
```

### Parser hit rate (regex vs. LLM vs. manual)

```sql
SELECT parser, COUNT(*) AS n, ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM expenses
WHERE deleted_at IS NULL
GROUP BY parser
ORDER BY n DESC;
```

### Full-text search across notes and raw messages

```sql
SELECT occurred_at, amount, category, note, raw_message
FROM expenses
WHERE deleted_at IS NULL
  AND (note LIKE '%kopi%' OR raw_message LIKE '%kopi%')
ORDER BY occurred_at DESC;
```

### Soft-deleted entries (what `/undo` has removed)

```sql
SELECT id, occurred_at, amount, category, note, deleted_at
FROM expenses
WHERE deleted_at IS NOT NULL
ORDER BY deleted_at DESC;
```

### Sanity check: no duplicate UUIDs

The `uuid` column is the idempotency key for the Sheets mirror — it should
never repeat. This should always return zero rows:

```sql
SELECT uuid, COUNT(*)
FROM expenses
GROUP BY uuid
HAVING COUNT(*) > 1;
```

---

## Reference: current schema

```sql
CREATE TABLE expenses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid          TEXT    NOT NULL UNIQUE,   -- idempotency key for Sheets sync
    occurred_at   TEXT    NOT NULL,          -- ISO8601 date, local-date semantics
    created_at    TEXT    NOT NULL,          -- ISO8601 UTC, insert time
    amount        INTEGER NOT NULL,          -- whole rupiah, never a float
    currency      TEXT    NOT NULL DEFAULT 'IDR',
    category      TEXT    NOT NULL,
    note          TEXT,
    raw_message   TEXT    NOT NULL,          -- original text, for re-parsing later
    parser        TEXT    NOT NULL,          -- 'regex' | 'llm' | 'manual'
    confidence    REAL,                      -- 0..1, parser's self-reported confidence
    sheet_synced  INTEGER NOT NULL DEFAULT 0,
    sheet_row     INTEGER,                   -- row number once written to the Sheet
    deleted_at    TEXT                       -- soft delete
);
```

Defined in `app/storage/db.py`. See the
[corrections & operations PRD](plan/2026-09-08-amna-corrections-and-operations-PRD.md)
for the `user_id` / `batch_id` columns planned for the next migration.
