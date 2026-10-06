"""Offline tests: isolate local files and never connect to Render."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.pop("DATABASE_URL", None)

import db
import app as web


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_URL", None)
    monkeypatch.setattr(db, "_HERE_DB", str(tmp_path))
    monkeypatch.setattr(db, "PROFILES_JSON_PATH", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(db, "ACTIVE_PROFILE_JSON_PATH", str(tmp_path / "active_profile.json"))
    monkeypatch.setattr(web, "CACHE_PATH", str(tmp_path / "portfolio_cache.json"))
    web.app.config.update(TESTING=True, SECURITY_DISABLED_FOR_TESTS=True)
    with web.app.test_client() as test_client:
        yield test_client


@pytest.fixture
def profile(client):
    response = client.post("/api/profiles", json={"name": "Test", "watchlist": []})
    assert response.status_code == 201
    return response.get_json()