from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    profile_url: str
    db_path: Path
    browser_profile: Path
    headless: bool
    dry_run: bool
    min_followers: int
    max_followers: int
    active_days: int
    min_score: float
    daily_limit: int
    weekly_limit: int
    delay_min: float
    delay_max: float
    artifact_dir: Path
    max_scrolls: int

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        return cls(
            profile_url=os.getenv("MEDIUM_PROFILE_URL", "https://medium.com/@nattupi"),
            db_path=Path(os.getenv("MEDIUM_DB_PATH", "data/medium_publisher.db")),
            browser_profile=Path(os.getenv("MEDIUM_BROWSER_PROFILE", "playwright-profile")),
            headless=_bool("MEDIUM_HEADLESS", False),
            dry_run=_bool("MEDIUM_DRY_RUN", True),
            min_followers=int(os.getenv("MEDIUM_MIN_FOLLOWERS", "100")),
            max_followers=int(os.getenv("MEDIUM_MAX_FOLLOWERS", "50000")),
            active_days=int(os.getenv("MEDIUM_ACTIVE_DAYS", "60")),
            min_score=float(os.getenv("MEDIUM_MIN_SCORE", "75")),
            daily_limit=int(os.getenv("MEDIUM_DAILY_LIMIT", "3")),
            weekly_limit=int(os.getenv("MEDIUM_WEEKLY_LIMIT", "10")),
            delay_min=float(os.getenv("MEDIUM_ACTION_DELAY_MIN", "2.0")),
            delay_max=float(os.getenv("MEDIUM_ACTION_DELAY_MAX", "5.0")),
            artifact_dir=Path(os.getenv("MEDIUM_ARTIFACT_DIR", "artifacts")),
            max_scrolls=int(os.getenv("MEDIUM_MAX_SCROLLS", "30")),
        )

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.browser_profile.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

