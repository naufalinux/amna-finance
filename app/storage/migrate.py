"""Apply pending migrations, then exit: `python -m app.storage.migrate`.

Deployment runs this with the service stopped, so the schema change happens
while there is exactly one writer touching the file.
"""

from app.config import get_settings
from app.logging_setup import configure_logging
from app.storage.db import init_db, run_migrations


def main() -> None:
    configure_logging()
    settings = get_settings()
    version = run_migrations(init_db(settings.db_path))
    print(f"{settings.db_path}: schema version {version}")


if __name__ == "__main__":
    main()
