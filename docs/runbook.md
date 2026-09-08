# Runbook

Operating the deployed agent: one systemd unit, one SQLite file, one Google
Sheet. Everything below assumes `/opt/expense-agent` and the `expense` service
user created by `deploy/install.sh`.

## At a glance

| Question | Answer |
|---|---|
| Is it running? | `make status` |
| What is it doing? | `make logs` |
| Is the data intact? | `/stats` in Telegram |
| Does the Sheet agree? | `/reconcile` in Telegram |
| Where are the backups? | `/opt/expense-agent/backups`, nightly at 03:00 |
| System of record | `/opt/expense-agent/data/expenses.db` — the Sheet is a mirror |

## Deploying a change

```
make deploy        # pull, install deps, migrate with the app stopped, restart
```

Migrations always run with the service stopped, so there is exactly one writer
on the file. `make deploy` does that for you; `make migrate` is the manual
equivalent.

## Restart, reboot and shutdown

`systemctl stop` sends `SIGTERM`, and the app handles it: it stops polling,
flushes anything still unsynced to the Sheet, checkpoints the WAL and exits 0.
`TimeoutStopSec=30` is the backstop if that stalls.

`Restart=always` with `RestartSec=10` brings it back after a crash, and
`WantedBy=multi-user.target` brings it back after a reboot.
`StartLimitBurst=5` in 300s stops a bad token or a corrupt database from
turning that into a hot crash-loop.

## Backups

Nightly at 03:00 local, into `/opt/expense-agent/backups`:

1. an online `sqlite3.Connection.backup()` snapshot — safe against the live
   WAL writer, no external binary needed inside the hardened unit,
2. `PRAGMA integrity_check` **on the copy**, before it is kept,
3. gzip to `expenses-YYYY-MM-DD.db.gz`,
4. prune to 14 daily plus the first backup of each of the last 12 months.

A corrupt copy is deleted and every owner gets a Telegram alert. Successes are
silent — check `/stats` for `Last backup`.

Force one now: `make backup-now`.

## Restore drill

Run this at least once, and after any change to the backup job. Target: under
five minutes.

```
systemctl stop expense-agent
gunzip -c /opt/expense-agent/backups/expenses-2026-09-07.db.gz > /tmp/restore.db
sqlite3 /tmp/restore.db "PRAGMA integrity_check;"     # expect: ok
mv /opt/expense-agent/data/expenses.db /opt/expense-agent/data/expenses.db.broken
mv /tmp/restore.db /opt/expense-agent/data/expenses.db
rm -f /opt/expense-agent/data/expenses.db-wal /opt/expense-agent/data/expenses.db-shm
chown expense:expense /opt/expense-agent/data/expenses.db
systemctl start expense-agent
```

`make restore BACKUP=<file>` does the same thing, keeping the old file as
`expenses.db.broken.<timestamp>`.

Then, in Telegram:

- `/stats` — the row count should match what you expect for that backup's date,
- `/reconcile` — the Sheet should still line up.

**Do not copy `expenses.db` alone from a running instance.** In WAL mode the
`-wal` and `-shm` siblings carry committed data; either take all three or use
the backup job, which handles this correctly.

## When the Sheet and the database disagree

`/reconcile` compares uuid sets in both directions and reports three things:

| Report | Means | Usual cause |
|---|---|---|
| Missing from the Sheet | we recorded a write that isn't there | someone deleted the row by hand |
| In the Sheet only | a row we don't know about | hand-typed, or orphaned by a manual reorder |
| Values disagree | amount, category or status differ | the Sheet was edited directly |

Reconciliation **reports and never repairs** — it cannot tell a deliberate
hand-edit from an accidental overwrite. Fix the Sheet by hand, or `/undo` and
re-log the entry so SQLite drives the correction.

A row whose uuid no longer matches its recorded position is handled
automatically: `sheet_row` is cleared and the row is re-appended at the bottom
on the next sweep, which may leave a stale orphan for `/reconcile` to report.

Never delete or reorder rows in the Sheet. Deleting row *N* shifts everything
below it up by one and invalidates every stored row number. Deleted entries
are represented by the `status` column, not by removing the row; the `Summary`
tab should filter on `status = "active"`.

## Logs

Structured lines go to stdout, systemd routes them to journald, and journald
rotates them. Retention is bounded by
`/etc/systemd/journald.conf.d/expense-agent.conf` (200 MB, one month). There is
no log file to fill the disk. `deploy/logrotate.expense-agent` exists for
anyone who prefers files, but it is not the default.

## Troubleshooting

| Symptom | Check |
|---|---|
| Unit won't start | `journalctl -u expense-agent -n 50`; usually `.env` or a bad token |
| `startup.bad_token` | Telegram rejected `TELEGRAM_BOT_TOKEN` |
| `sheets.disabled` at startup | `GOOGLE_SHEET_ID` or the credentials file is missing |
| `backup.disabled` at startup | `BACKUP_PATH` not set |
| `/stats` shows a growing unsynced count | Sheets API failing; `make logs` for `sheets.*_failed` |
| Commands ignored | your Telegram id is not in `TELEGRAM_OWNER_ID` (see `auth.rejected`) |
| Permission errors writing the DB | the path must be inside the unit's `ReadWritePaths` |
| A syscall is blocked | `SystemCallFilter=@system-service` is permissive, but check the journal for `EPERM` |
