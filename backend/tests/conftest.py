import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Configure isolated test environment BEFORE importing app modules
_TMP_DIR = tempfile.mkdtemp(prefix="ncke_test_")
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR}/test.db"
os.environ["STORAGE_ROOT"] = f"{_TMP_DIR}/storage"
os.environ["DEEPSEEK_MOCK_MODE"] = "true"
os.environ["DEEPSEEK_API_KEY"] = ""

from app.core.config import get_settings  # noqa: E402
from app.infrastructure.db.session import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def reset_db():
    get_settings.cache_clear()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    Path(os.environ["STORAGE_ROOT"]).mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c
