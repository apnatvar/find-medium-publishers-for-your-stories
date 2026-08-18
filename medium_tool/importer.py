from __future__ import annotations

from urllib.parse import urlparse

from .browser import BrowserSession
from .config import Settings
from .db import Database
from .parsers import parse_profile_story_links, parse_story


class StoryImporter:
    def __init__(self, settings: Settings, db: Database, browser: BrowserSession):
        self.settings, self.db, self.browser = settings, db, browser

    async def sync(self) -> dict:
        page = await self.browser.page(self.settings.profile_url, "sync-profile")
        try:
            previous_height = 0
            stable = 0
            for _ in range(self.settings.max_scrolls):
                height = await page.evaluate("document.body.scrollHeight")
                if height == previous_height:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0
                previous_height = height
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1200)
            links = parse_profile_story_links(await page.content(), self.settings.profile_url)
        finally:
            await page.close()
        handle = urlparse(self.settings.profile_url).path.strip("/").lstrip("@")
        imported, failures = [], []
        for url in links:
            story_page = None
            try:
                story_page = await self.browser.page(url, "sync-story")
                story = parse_story(await story_page.content(), url, handle)
                story["source_html"] = None
                story_id = self.db.upsert_story(story)
                imported.append({"id": story_id, **story})
                self.browser.audit("read-story", "success", target_type="story", target_id=story_id, destination=url)
            except Exception as exc:
                failures.append({"url": url, "error": str(exc)})
            finally:
                if story_page:
                    await story_page.close()
            await self.browser.delay()
        return {"found": len(links), "imported": imported, "failures": failures}

