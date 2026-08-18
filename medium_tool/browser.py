from __future__ import annotations

import asyncio
import json
import random
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from .config import Settings
from .db import Database, utcnow


class ManualIntervention(RuntimeError):
    """Raised when automation must stop for a human decision."""


class BrowserSession:
    BLOCKING_TEXT = (
        "captcha", "verify you are human", "security check", "unusual activity",
        "confirm your identity",
    )

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.context = None

    @asynccontextmanager
    async def open(self) -> AsyncIterator["BrowserSession"]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed. Run: pip install -r requirements.txt") from exc
        manager = async_playwright()
        playwright = await manager.start()
        self.context = await playwright.chromium.launch_persistent_context(
            str(self.settings.browser_profile.resolve()),
            headless=self.settings.headless,
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            yield self
        finally:
            await self.context.close()
            await playwright.stop()
            self.context = None

    async def page(self, url: str, operation: str):
        assert self.context is not None
        page = await self.context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1200)
            await self.ensure_safe(page, operation)
            return page
        except Exception as exc:
            await self.capture_failure(page, operation, exc)
            await page.close()
            raise

    async def ensure_safe(self, page, operation: str) -> None:
        body = (await page.locator("body").inner_text(timeout=10_000)).lower()
        if any(marker in body for marker in self.BLOCKING_TEXT):
            await self.capture_failure(page, operation, ManualIntervention("Security challenge detected"))
            raise ManualIntervention("Medium displayed a CAPTCHA or security prompt; complete it manually.")

    async def delay(self) -> None:
        await asyncio.sleep(random.uniform(self.settings.delay_min, self.settings.delay_max))

    async def capture_failure(self, page, operation: str, exc: Exception) -> tuple[str | None, str | None]:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_name = "".join(c if c.isalnum() else "-" for c in operation)[:60]
        screenshot = self.settings.artifact_dir / f"{stamp}-{safe_name}.png"
        snapshot = self.settings.artifact_dir / f"{stamp}-{safe_name}.html"
        screenshot_path = snapshot_path = None
        try:
            await page.screenshot(path=str(screenshot), full_page=True)
            screenshot_path = str(screenshot)
        except Exception:
            pass
        try:
            snapshot.write_text(await page.content(), encoding="utf-8")
            snapshot_path = str(snapshot)
        except Exception:
            pass
        self.db.execute(
            """INSERT INTO errors(operation,message,traceback,screenshot_path,html_snapshot_path,created_at)
               VALUES(?,?,?,?,?,?)""",
            (operation, str(exc), traceback.format_exc(), screenshot_path, snapshot_path, utcnow()),
        )
        return screenshot_path, snapshot_path

    def audit(
        self, action: str, result: str, *, target_type: str | None = None,
        target_id: int | None = None, destination: str | None = None,
        details: dict | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO browser_actions(
                action,target_type,target_id,destination,dry_run,result,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                action, target_type, target_id, destination, int(self.settings.dry_run),
                result, json.dumps(details or {}), utcnow(),
            ),
        )

