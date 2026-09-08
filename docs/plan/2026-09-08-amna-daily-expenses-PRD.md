# PRD — Telegram Daily Expense Recap Agent

**Status:** Draft v1.0
**Owner:** Personal project (single user)
**Target runtime:** Self-hosted on cloud VPS
**Last updated:** 2026-09-08

---

## 1. Overview

A personal Telegram bot that lets you log expenses by sending natural-language messages ("grab 45k, kopi 20k"), writes them to Google Sheets as structured rows, and sends you a summarized recap every evening.

The system is deliberately small: one long-running Python process on your VPS, no public HTTP endpoint, no webhook/SSL setup, no external orchestration platform.

### Goals

- Log an expense in under 5 seconds from the Telegram compose box, with no rigid syntax.
- Maintain a Google Sheet as the human-readable, portable source of record.
- Deliver an automatic daily recap (total, per-category breakdown, notable spends).
- Stay cheap: target < $1/month in LLM costs at personal volume.
- Be resilient: a failed Sheets API call must never silently lose an expense.

### Non-goals (v1)

- Multi-user / shared household budgets.
- Receipt photo OCR.
- Bank/e-wallet transaction import.
- A web dashboard or mobile app.
- Multi-currency conversion (single currency assumed; see §9 Future Work).

### Success criteria

| Metric | Target |
|---|---|
| Parse accuracy on typical messages | ≥ 95% correct amount + category |
| Expense loss rate | 0 — every accepted message is eventually persisted |
| Daily recap delivery | 100% on schedule, ≥ 30 consecutive days |
| Median log-to-confirmation latency | < 3s |

---

## 2. User stories

1. **Quick log** — I send `gojek 25k` and get a confirmation within seconds showing what was recorded.
2. **Multi-item log** — I send `lunch 45k, kopi 20k, parkir 5k` and all three are recorded as separate rows.
3. **Correction** — I realize an amount was wrong and can fix or delete the last entry without opening the Sheet.
4. **Daily recap** — At 21:00 local time I receive a message with today's total, a per-category breakdown, and a short note on anything unusual.
5. **Ad-hoc query** — I send `/today` or `/week` and get an on-demand summary.
6. **Trust the record** — I can open the Google Sheet any time and see clean, structured, sorted data.

---

## 3. Architecture

```
                        ┌─────────────────────────────┐
   Telegram (you) ──────►  aiogram bot (long-polling) │
        ▲                └──────────────┬──────────────┘
        │                               │
        │                    ┌──────────▼──────────┐
        │                    │  Parser service     │
        │                    │  regex fast-path    │
        │                    │  → LLM fallback     │
        │                    └──────────┬──────────┘
        │                               │ ExpenseEntry[]
        │                    ┌──────────▼──────────┐
        │                    │  Repository layer   │
        │                    │  (write-through)    │
        │                    └─────┬──────────┬────┘
        │                          │          │
        │              ┌───────────▼──┐   ┌───▼────────────┐
        │              │ SQLite (WAL) │   │ Google Sheets  │
        │              │ system of    │   │ mirror / human │
        │              │ record       │   │ view           │
        │              └───────▲──────┘   └────────────────┘
        │                      │
        │            ┌─────────┴──────────┐
        └────────────┤ APScheduler        │
       daily recap   │  21:00 recap job   │
                     │  */5m sync retry   │
                     └────────────────────┘
```

### Design decisions

| Decision | Rationale |
|---|---|
| **Long-polling, not webhooks** | No public URL, no TLS cert, no reverse proxy on the VPS. One less failure mode. |
| **SQLite as system of record from day 1** | Sheets API is rate-limited and network-dependent. Local write is instant and always succeeds; Sheets becomes an async mirror. This is the key reliability decision — see §6. |
| **Regex fast-path before LLM** | Most messages (`kopi 20k`) match a simple pattern. Skipping the LLM makes the common case free and sub-second; LLM handles the messy tail. |
| **In-process APScheduler, not system cron** | One deployable unit, one set of credentials, one log stream. No cron/env/permission mismatch. |
| **Single process, no queue/broker** | At personal scale (tens of messages/day) Redis or Celery is pure overhead. |

---

## 4. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | Native `tomllib`, better async perf, `datetime.UTC` |
| Telegram | `aiogram` 3.x | Async, modern API, first-class long-polling, clean router/handler model |
| Local DB | SQLite via `sqlite3` (stdlib) + `SQLModel` or raw SQL | WAL mode enabled; single-writer is fine here |
| Migrations | `alembic` (if SQLModel/SQLAlchemy) or hand-rolled `schema_version` table | Keep it simple; a version table is enough for v1 |
| Sheets | `gspread` + Google service account | Share the Sheet with the service account email; JSON key on disk, chmod 600 |
| LLM parsing | Anthropic Claude Haiku *or* OpenAI `gpt-4o-mini` via `httpx` | Cheap, fast, structured JSON output. Prompt pins a strict JSON schema. |
| Scheduling | `APScheduler` (AsyncIOScheduler) | Cron-style triggers inside the event loop |
| Config | `pydantic-settings` + `.env` | Typed config, fails loudly on missing keys |
| Logging | `structlog` or stdlib `logging` with JSON formatter | Goes to journald via systemd |
| Process mgmt | `systemd` unit (or Docker `restart: unless-stopped`) | Auto-restart on crash and reboot |
| Testing | `pytest` + `pytest-asyncio` | Parser test suite is the highest-value tests |

### Dependencies (`requirements.txt`)

```
aiogram>=3.13
gspread>=6.1
google-auth>=2.35
apscheduler>=3.10
pydantic>=2.9
pydantic-settings>=2.5
httpx>=0.27
sqlmodel>=0.0.22
structlog>=24.4
python-dateutil>=2.9
```

---

## 5. Project structure

```
expense-agent/
├── app/
│   ├── __init__.py
│   ├── main.py              # entrypoint: wire bot + scheduler, run event loop
│   ├── config.py            # pydantic-settings, loads .env
│   ├── bot/
│   │   ├── handlers.py      # message + command handlers
│   │   └── formatting.py    # message templates (confirmations, recaps)
│   ├── parsing/
│   │   ├── regex_parser.py  # fast-path pattern matching
│   │   ├── llm_parser.py    # LLM fallback, strict JSON schema
│   │   └── categories.py    # category taxonomy + keyword hints
│   ├── storage/
│   │   ├── models.py        # ExpenseEntry, SyncState
│   │   ├── db.py            # SQLite engine, WAL setup, migrations
│   │   ├── sheets.py        # gspread client, append/read
│   │   └── repository.py    # write-through facade used by everything else
│   ├── jobs/
│   │   ├── daily_recap.py   # 21:00 summary job
│   │   └── sync_retry.py    # */5min flush unsynced rows to Sheets
│   └── integrations/
│       └── hermes.py        # HTTP/MCP surface for Hermes Agent (see §8)
├── tests/
│   ├── test_regex_parser.py
│   ├── test_repository.py
│   └── fixtures/messages.yaml
├── deploy/
│   ├── expense-agent.service
│   └── docker-compose.yml
├── .env.example
├── requirements.txt
└── README.md
```

---

## 6. Data layer: SQLite + Google Sheets

This is the part worth getting right. **SQLite is the system of record; Google Sheets is a mirror.**

### Why both

Google Sheets alone is fragile as a primary store: API calls can fail or rate-limit, reads are slow for aggregation, there are no transactions, and querying "sum by category for the last 30 days" means pulling the whole sheet. But Sheets is genuinely valuable as a human interface — you can open it on your phone, chart it, share it, and it survives even if the bot dies.

So: write locally first (always succeeds, instant), then mirror to Sheets asynchronously with retry.

### Schema

```sql
CREATE TABLE expenses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid          TEXT    NOT NULL UNIQUE,   -- idempotency key for Sheets sync
    occurred_at   TEXT    NOT NULL,          -- ISO8601, local date semantics
    created_at    TEXT    NOT NULL,          -- ISO8601 UTC, insert time
    amount        INTEGER NOT NULL,          -- minor units (cents/rupiah), never float
    currency      TEXT    NOT NULL DEFAULT 'IDR',
    category      TEXT    NOT NULL,
    note          TEXT,
    raw_message   TEXT    NOT NULL,          -- original text, for re-parsing later
    parser        TEXT    NOT NULL,          -- 'regex' | 'llm' | 'manual'
    confidence    REAL,                      -- 0..1, LLM self-reported
    sheet_synced  INTEGER NOT NULL DEFAULT 0,
    sheet_row     INTEGER,                   -- row number once written
    deleted_at    TEXT                       -- soft delete
);

CREATE INDEX idx_expenses_occurred ON expenses(occurred_at) WHERE deleted_at IS NULL;
CREATE INDEX idx_expenses_unsynced ON expenses(sheet_synced) WHERE sheet_synced = 0;

CREATE TABLE sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE schema_version (
    version     INTEGER NOT NULL,
    applied_at  TEXT NOT NULL
);
```

**Money is stored as integer minor units.** Never floats. `45000` not `45000.0`.

### Write-through flow

1. Parser produces one or more `ExpenseEntry` objects.
2. `repository.add()` inserts into SQLite inside a transaction → returns immediately.
3. Bot confirms to the user (fast — user is never waiting on Google).
4. A background task attempts the Sheets append; on success sets `sheet_synced = 1`, `sheet_row = N`.
5. If it fails, the row stays `sheet_synced = 0`. The `sync_retry` job (every 5 min) picks up all unsynced rows and retries with exponential backoff.
6. `uuid` in a hidden Sheet column makes the sync idempotent — a retry after an ambiguous failure can't create a duplicate.

### Sheet layout

| A: uuid | B: date | C: amount | D: currency | E: category | F: note | G: raw | H: parser |
|---|---|---|---|---|---|---|---|

Column A can be hidden in the UI. A second tab (`Summary`) holds pivot/chart formulas that reference the raw tab — regenerated by Sheets itself, not by the bot.

### Migration path (staged)

The DB layer is designed to arrive in stages so v1 ships fast:

**Stage 0 — Sheets only (skip if you want, but don't).**
Direct `gspread` append on every message. Simplest possible thing. Works, but loses entries on network blips.

**Stage 1 — SQLite added as write-ahead buffer.** *(recommended v1 scope)*
Introduce `repository.py` as the only thing the bot talks to. It writes SQLite first, then fires the Sheets append. Existing Sheet rows can be backfilled into SQLite once with a one-off script that reads the whole sheet and assigns uuids.

**Stage 2 — SQLite becomes the read path.**
Recap jobs and `/today`, `/week` queries stop reading from Sheets and run SQL aggregations instead. This is where it gets noticeably faster and where interesting queries become trivial:

```sql
SELECT category, SUM(amount) AS total, COUNT(*) AS n
FROM expenses
WHERE occurred_at >= date('now','localtime','-30 days')
  AND deleted_at IS NULL
GROUP BY category
ORDER BY total DESC;
```

Sheets is now purely an output mirror.

**Stage 3 — optional Postgres swap.**
Only if you ever need concurrent writers (multi-user, or Hermes writing at the same time as the bot). Keep all queries in `repository.py` and behind SQLModel/SQLAlchemy so the swap is a connection-string change plus a migration, not a rewrite. Realistically: you will not need this for a personal tracker.

### Backup

- `sqlite3 expenses.db ".backup /backups/expenses-$(date +%F).db"` nightly via the scheduler.
- Keep 14 daily + 12 monthly. The DB will be a few MB at most.
- Google Sheets is itself a de-facto offsite backup — which is a nice side effect of the mirror design.

---

## 7. Core workflows

### 7.1 Logging an expense

```
User: "grab 45k, kopi hitam 20rb"
  │
  ├─ handler receives message
  ├─ regex_parser: splits on comma, matches (\d+)\s*(k|rb|ribu|jt)? patterns
  │    → 2 entries parsed with high confidence → skip LLM
  ├─ categories.infer(): "grab" → Transport, "kopi" → Food & Drink
  ├─ repository.add_many([...]) → SQLite commit
  ├─ reply: "✅ Logged 2 items — Rp65.000
  │           • Transport  Rp45.000  grab
  │           • Food       Rp20.000  kopi hitam"
  └─ background: append 2 rows to Sheets, mark synced
```

If the regex parser produces low confidence or finds no amount, it falls through to the LLM parser, which is prompted to return strict JSON:

```json
{"entries":[{"amount":45000,"currency":"IDR","category":"Transport",
  "note":"grab","confidence":0.95}]}
```

Ambiguous results (`confidence < 0.7`) trigger an inline-keyboard confirmation instead of a silent write.

### 7.2 Daily recap (21:00 local)

```
APScheduler cron trigger
  ├─ repository.entries_for_day(today)
  ├─ if empty → send "No expenses logged today 🎉" and stop
  ├─ compute: total, per-category totals, largest single item,
  │           delta vs. 7-day average
  ├─ (optional) LLM pass for a one-line human comment
  └─ send formatted message to TELEGRAM_OWNER_ID
```

Example output:

```
📊 Tuesday, 8 Sep — Rp247.000

  Food & Drink    Rp112.000  (5)
  Transport        Rp85.000  (3)
  Groceries        Rp50.000  (1)

Largest: Rp50.000 — Superindo
21% above your 7-day average.
```

### 7.3 Commands

| Command | Behavior |
|---|---|
| `/today` | On-demand version of the daily recap |
| `/week`, `/month` | Aggregate over the period, per-category |
| `/undo` | Soft-delete the most recent entry; also strikes/removes the Sheet row |
| `/edit <amount>` | Correct the amount on the last entry |
| `/cat <category>` | Recategorize the last entry |
| `/sync` | Force a Sheets sync flush; report unsynced count |
| `/stats` | DB row count, last sync time, parser hit rates |

### 7.4 Failure handling

| Failure | Behavior |
|---|---|
| Sheets API down/rate-limited | Entry already in SQLite; retry job flushes later. User never notices. |
| LLM API down | Fall back to regex-only; if that fails, store `raw_message` with `category='Uncategorized'` and flag for review. Never drop the message. |
| Telegram connection drop | aiogram reconnects automatically; long-polling resumes from last offset. |
| Process crash | systemd restarts; SQLite WAL guarantees no partial writes. |
| Bad parse | `/undo` + `/edit`; `raw_message` is retained so entries can be re-parsed in bulk after a parser improvement. |

---

## 8. Hermes Agent integration

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is a general-purpose autonomous agent with persistent memory, tool use, cron scheduling, and multi-platform messaging. The expense agent is a *narrow, deterministic* service; Hermes is a *broad, conversational* one. The integration principle: **Hermes never owns the data — it queries and annotates.**

### Why keep them separate

The expense bot must be boring and reliable: same input, same row, every time. An LLM agent deciding how to record an expense introduces nondeterminism into your financial record. So the expense agent keeps exclusive write authority over the DB and Sheet, and Hermes gets a constrained interface to it.

### Integration options

**Option A — Expose a local HTTP API (simplest).**

Add a small FastAPI app inside the same process (or a sibling service) bound to `127.0.0.1` only:

```
GET  /api/expenses?from=2026-09-01&to=2026-09-08&category=Food
GET  /api/summary?period=week
GET  /api/stats
POST /api/expenses          # optional, requires shared-secret header
```

Hermes calls these via its HTTP tool. Read endpoints are unauthenticated on loopback; the write endpoint requires a bearer token from `.env`. This is the lowest-effort path and keeps the boundary crisp.

**Option B — MCP server (cleanest for agent use).**

Wrap the repository in an MCP server exposing typed tools:

| Tool | Purpose |
|---|---|
| `query_expenses` | Filtered fetch by date range / category / amount |
| `summarize_period` | Pre-aggregated totals — cheaper than making Hermes sum raw rows |
| `log_expense` | Structured write (guarded, idempotent via uuid) |
| `get_budget_status` | Spend vs. budget for the current month |

Hermes connects to it as a tool provider. This is better than Option A because the tool schemas constrain what Hermes can do and how it interprets results, and `summarize_period` prevents Hermes from pulling thousands of raw rows into context.

**Option C — Shared read-only SQLite handle.**

Give Hermes read-only access to the same DB file (`file:expenses.db?mode=ro`). Zero API surface, but Hermes then needs to know your schema, and schema changes silently break it. Use only if you want Hermes writing ad-hoc SQL for exploratory questions.

**Recommendation:** Start with **A**, move to **B** once you're actually using Hermes daily. Both can coexist — the MCP server can be a thin wrapper over the same repository functions the HTTP API uses.

### What Hermes adds on top

Once wired, you get things the deterministic bot shouldn't do itself:

- **Conversational analysis** — "why was last week expensive?" → Hermes queries, compares to prior weeks, explains.
- **Cross-domain reasoning** — Hermes has your calendar/notes context; it can connect a spending spike to a trip.
- **Proactive cron** — Hermes's scheduler can run a weekly deep-dive that's more analytical than the bot's mechanical daily recap.
- **Memory** — Hermes remembers your stated goals ("cut coffee spend") and references them unprompted.
- **Natural corrections** — "that Grab yesterday was actually for work" → Hermes calls `log_expense`/update with the right tags.

### Boundary rules

1. Hermes has **read by default**; writes require an explicit token and always go through the repository's validation, never raw SQL.
2. Every Hermes-originated write sets `parser = 'hermes'` so you can audit and, if needed, bulk-revert.
3. The daily recap stays in the Python bot. If Hermes is down or hallucinating, your core loop still works.
4. Run both on the same VPS but as separate systemd units — Hermes is resource-hungry (its docs suggest multi-core / several GB of RAM), and it must not be able to take the expense bot down with it.

---

## 9. Deployment

### systemd unit (`deploy/expense-agent.service`)

```ini
[Unit]
Description=Telegram Expense Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=expense
WorkingDirectory=/opt/expense-agent
EnvironmentFile=/opt/expense-agent/.env
ExecStart=/opt/expense-agent/.venv/bin/python -m app.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/expense-agent/data

[Install]
WantedBy=multi-user.target
```

### Environment (`.env.example`)

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_ID=            # numeric; bot ignores everyone else
GOOGLE_SHEET_ID=
GOOGLE_CREDENTIALS_PATH=/opt/expense-agent/secrets/service-account.json
LLM_PROVIDER=anthropic        # anthropic | openai
LLM_API_KEY=
LLM_MODEL=claude-haiku-4-5-20251001
DB_PATH=/opt/expense-agent/data/expenses.db
TIMEZONE=Asia/Jakarta
RECAP_TIME=21:00
DEFAULT_CURRENCY=IDR
HERMES_API_TOKEN=             # only if exposing write endpoint
```

### Security

- Bot rejects any `from_user.id != TELEGRAM_OWNER_ID` — the single most important line of code in the project.
- Service account JSON `chmod 600`, owned by the service user, outside any git repo.
- Sheet shared with the service account only, not "anyone with link."
- No inbound ports opened. If you add the Hermes HTTP API, bind `127.0.0.1` only.
- `.env` never committed; `.gitignore` covers `*.json`, `*.db`, `.env`.

---

## 10. Build plan

| Phase | Scope | Done when |
|---|---|---|
| **P0** | Skeleton: config, aiogram bot, owner-ID guard, echo handler | Bot replies to you and ignores others |
| **P1** | Regex parser + category inference + test fixtures | Parser suite passes on ~40 real message samples |
| **P2** | SQLite schema, repository, write-through insert | Expenses persist across restarts |
| **P3** | Sheets mirror + sync retry job | Kill network, log expenses, restore — all rows appear |
| **P4** | LLM fallback parser with JSON schema + confidence gate | Messy messages parse correctly; low-confidence prompts confirmation |
| **P5** | APScheduler daily recap + `/today` `/week` | Recap arrives on time for 7 straight days |
| **P6** | `/undo` `/edit` `/cat` `/sync` `/stats` | Corrections work end-to-end incl. Sheet |
| **P7** | systemd unit, backups, log rotation | Survives `reboot` unattended |
| **P8** | Hermes integration (Option A → B) | Hermes can answer "how much on food this month?" |

P0–P5 is a genuinely usable product. P6–P8 is polish and extension.

---

## 11. Future work

- **Budgets & alerts** — monthly caps per category, warn at 80%.
- **Receipt photos** — Telegram photo handler → vision model → same parser pipeline. `raw_message` becomes the image reference.
- **Multi-currency** — add `amount_base` + `fx_rate` columns; nightly rate fetch. Schema already has `currency` so this is additive.
- **Income/transfers** — add a `kind` column (`expense` | `income` | `transfer`) rather than negative amounts.
- **Recurring detection** — flag subscriptions from repeated same-amount same-category entries.
- **Re-parse pipeline** — since `raw_message` is retained, improving the parser lets you retroactively fix historical categorization.
- **Charts in Telegram** — matplotlib → PNG → send as photo with the weekly recap.
