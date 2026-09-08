# Operations for the deployed agent. Run on the VPS unless noted.
#
# APP_DIR is where install.sh puts things; override it for a different layout.
APP_DIR ?= /opt/expense-agent
UNIT    ?= expense-agent.service
PYTHON  ?= $(APP_DIR)/.venv/bin/python
DB      ?= $(APP_DIR)/data/expenses.db
BACKUPS ?= $(APP_DIR)/backups

.PHONY: help install deploy migrate logs status test backup-now restore

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## First-time install: user, venv, unit, journald, migrate, enable
	sudo deploy/install.sh

deploy: ## Pull, reinstall deps, migrate with the app stopped, restart
	git pull --ff-only
	sudo systemctl stop $(UNIT)
	$(APP_DIR)/.venv/bin/pip install --quiet -r $(APP_DIR)/requirements.txt
	sudo -u expense $(PYTHON) -m app.storage.migrate
	sudo systemctl start $(UNIT)
	@sleep 2 && systemctl status $(UNIT) --no-pager

migrate: ## Apply pending migrations (app must be stopped)
	sudo -u expense $(PYTHON) -m app.storage.migrate

logs: ## Follow the journal
	journalctl -u $(UNIT) -f -n 100

status: ## Unit state plus the last 20 lines
	@systemctl status $(UNIT) --no-pager || true
	@journalctl -u $(UNIT) -n 20 --no-pager

test: ## Run the test suite (dev machine)
	.venv/bin/python -m pytest -q

backup-now: ## Take a verified backup immediately
	sudo -u expense $(PYTHON) -c \
		"from datetime import date; from pathlib import Path; \
		 from app.config import get_settings; from app.jobs.backup import run_backup; \
		 s = get_settings(); \
		 print(run_backup(s.db_path, s.backup_path or Path('$(BACKUPS)'), date.today(), \
		                  s.backup_keep_daily, s.backup_keep_monthly))"

restore: ## Restore from a backup: make restore BACKUP=backups/expenses-2026-09-07.db.gz
ifndef BACKUP
	$(error set BACKUP=<file>, e.g. make restore BACKUP=$(BACKUPS)/expenses-2026-09-07.db.gz)
endif
	sudo systemctl stop $(UNIT)
	gunzip -c $(BACKUP) > /tmp/restore.db
	sqlite3 /tmp/restore.db "PRAGMA integrity_check;"
	sudo -u expense cp $(DB) $(DB).broken.$$(date +%s)
	sudo -u expense install -m 0600 -o expense -g expense /tmp/restore.db $(DB)
	sudo -u expense rm -f $(DB)-wal $(DB)-shm
	rm -f /tmp/restore.db
	sudo systemctl start $(UNIT)
	@echo "Restored. Check /stats and /reconcile in Telegram."
