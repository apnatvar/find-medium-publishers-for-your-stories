from pathlib import Path

import pytest

from medium_tool.config import Settings
from medium_tool.db import Database


@pytest.fixture
def settings(tmp_path):
    return Settings(
        profile_url="https://medium.com/@nattupi",
        db_path=tmp_path / "test.db",
        browser_profile=tmp_path / "profile",
        headless=True,
        dry_run=True,
        min_followers=100,
        max_followers=50_000,
        active_days=60,
        min_score=75,
        daily_limit=3,
        weekly_limit=10,
        delay_min=0,
        delay_max=0,
        artifact_dir=tmp_path / "artifacts",
        max_scrolls=2,
    )


@pytest.fixture
def db(settings):
    settings.ensure_dirs()
    database = Database(settings.db_path)
    database.migrate()
    return database


@pytest.fixture
def fixture_dir():
    return Path(__file__).parent / "fixtures"

