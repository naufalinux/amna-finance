from pathlib import Path

import pytest
import yaml

FIXTURES = Path(__file__).parent / "fixtures"


def load_messages() -> list[dict]:
    return yaml.safe_load((FIXTURES / "messages.yaml").read_text())


@pytest.fixture
def repository(tmp_path):
    from app.storage.db import init_db
    from app.storage.repository import Repository

    return Repository(init_db(tmp_path / "test.db"), timezone="Asia/Jakarta")
