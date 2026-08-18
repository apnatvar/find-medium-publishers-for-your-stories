from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from .browser import BrowserSession
from .config import Settings
from .db import Database, utcnow
from .parsers import canonical_medium_url, parse_guidelines, parse_publication


OFFICIAL_START_URLS = [
    "https://help.medium.com/hc/en-us/articles/23964242497559-Browse-100-publications-in-the-Boost-Nomination-Program",
]

RELEVANT_TOPICS = {
    "philosophy", "psychology", "personal", "reflective", "life", "self",
    "human", "behavior", "behaviour", "development", "growth", "relationships",
    "culture", "society", "mental health", "wellness", "mindfulness",
}


class PublicationDiscovery:
    def __init__(self, settings: Settings, db: Database, browser: BrowserSession):
        self.settings, self.db, self.browser = settings, db, browser

    async def discover(self, start_urls: list[str] | None = None) -> dict:
        starts = start_urls or OFFICIAL_START_URLS
        candidates: dict[str, dict] = {}
        failures = []
        for start in starts:
            page = None
            try:
                page = await self.browser.page(start, "discover-source")
                for _ in range(5):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(700)
                for item in await self._candidate_links(page):
                    candidates[item["url"]] = item
            except Exception as exc:
                failures.append({"url": start, "error": str(exc)})
            finally:
                if page:
                    await page.close()
        verified = []
        for url, seed in candidates.items():
            try:
                publication = await self.verify(
                    url, seed.get("guideline_url"), seed.get("accepted_topics", [])
                )
                if publication:
                    verified.append(publication)
            except Exception as exc:
                failures.append({"url": url, "error": str(exc)})
            await self.browser.delay()
        return {"candidate_count": len(candidates), "verified": verified, "failures": failures}

    async def verify(
        self, url: str, guideline_url: str | None = None,
        seed_topics: list[str] | None = None,
    ) -> dict | None:
        page = await self.browser.page(url, "verify-publication")
        try:
            publication = parse_publication(await page.content(), url)
            publication["accepted_topics"] = list(dict.fromkeys(
                [*publication.get("accepted_topics", []), *(seed_topics or [])]
            ))
            links = await page.locator("a[href]").evaluate_all(
                """els => els.map(a => ({href:a.href, text:(a.innerText||'').trim()}))"""
            )
            detected_guideline = guideline_url or self._find_guideline(links)
            latest = publication.get("last_publication_date")
            publication["status"] = self._activity_status(latest)
            publication["latest_verification_date"] = utcnow()
            publication["guideline_url"] = detected_guideline
            publication.setdefault("accepts_submissions", None)
            if detected_guideline:
                rules = await self._verify_guideline(detected_guideline, publication)
                rule_topics = rules.pop("accepted_topics", [])
                rules.pop("raw_text", None)
                publication.update(rules)
                publication["accepted_topics"] = list(dict.fromkeys(
                    [*publication["accepted_topics"], *rule_topics]
                ))
            publication_id = self.db.upsert_publication(publication)
            publication["id"] = publication_id
            self.browser.audit(
                "verify-publication", "success", target_type="publication",
                target_id=publication_id, destination=url, details={"status": publication["status"]},
            )
            return publication
        finally:
            await page.close()

    async def _verify_guideline(self, url: str, publication: dict) -> dict:
        page = await self.browser.page(url, "verify-guidelines")
        try:
            html = await page.content()
            rules = parse_guidelines(html)
            if any(host in urlparse(url).netloc.lower() for host in ("docs.google.com", "forms.gle", "typeform")):
                rules["onboarding_method"] = "external-form"
                rules["onboarding_destination"] = url
                rules["requires_application"] = True
                rules["is_open_to_all"] = False
            raw = rules["raw_text"]
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            pub_id = self.db.upsert_publication(publication)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            snapshot = self.settings.artifact_dir / f"guideline-{pub_id}-{stamp}.html"
            snapshot.write_text(html, encoding="utf-8")
            self.db.execute(
                """INSERT OR IGNORE INTO guidelines(
                    publication_id,url,content_hash,extracted_text,rules_json,verified_at,html_snapshot_path
                ) VALUES(?,?,?,?,?,?,?)""",
                (pub_id, url, digest, raw, json.dumps({k: v for k, v in rules.items() if k != "raw_text"}), utcnow(), str(snapshot)),
            )
            return rules
        finally:
            await page.close()

    async def _candidate_links(self, page) -> list[dict]:
        links = await page.locator("a[href]").evaluate_all(
            """els => els.map(a => ({
                href:a.href,
                text:(a.innerText||'').trim(),
                context:(a.closest('tr')?.innerText||a.parentElement?.parentElement?.innerText||a.parentElement?.innerText||'').trim(),
                rowLinks:Array.from(a.closest('tr')?.querySelectorAll('a[href]')||[]).map(x => x.href),
                cells:Array.from(a.closest('tr')?.querySelectorAll('td')||[]).map(x => (x.innerText||'').trim())
            }))"""
        )
        results = []
        for link in links:
            href = canonical_medium_url(link["href"])
            parsed = urlparse(href)
            own_text = link["text"].lower()
            combined = f"{own_text} {link.get('context', '')}".lower()
            if not re.search(r"\b(submit|submission|guideline|write for)\b", own_text):
                continue
            if not any(topic in combined for topic in RELEVANT_TOPICS):
                continue
            publication_url = None
            for row_link in link.get("rowLinks", []):
                canonical_row_link = canonical_medium_url(row_link)
                if canonical_row_link != href and self._looks_like_publication_home(canonical_row_link):
                    publication_url = canonical_row_link
                    break
            publication_url = publication_url or self._publication_url_from_guideline(href)
            if publication_url:
                topic_text = link.get("cells", ["", ""])[1] if len(link.get("cells", [])) > 1 else ""
                seed_topics = [
                    re.sub(r"\s+", " ", topic.strip().lower())
                    for topic in topic_text.split(",") if topic.strip()
                ]
                results.append({
                    "url": publication_url, "guideline_url": href,
                    "accepted_topics": seed_topics,
                })
        return list({x["url"]: x for x in results}.values())[:50]

    @staticmethod
    def _looks_like_publication_home(url: str) -> bool:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.netloc.lower() == "medium.com":
            return len(parts) == 1 and not parts[0].startswith("@")
        return bool(parsed.netloc) and not parsed.netloc.lower().endswith("help.medium.com")

    @staticmethod
    def _publication_url_from_guideline(url: str) -> str | None:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        parts = [part for part in parsed.path.split("/") if part]
        if not host or host in {"help.medium.com", "policy.medium.com"}:
            return None
        if host.endswith(".medium.com") and host != "medium.com":
            return f"{parsed.scheme}://{parsed.netloc}"
        if host == "medium.com":
            if len(parts) < 2 or parts[0].startswith("@") or parts[0] in {"m", "tag", "search", "sitemap"}:
                return None
            return f"{parsed.scheme}://{parsed.netloc}/{parts[0]}"
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _find_guideline(links: list[dict]) -> str | None:
        for link in links:
            if re.search(r"(submit|submission|guideline|write for us|become a writer)", link["text"] + " " + link["href"], re.I):
                return canonical_medium_url(link["href"])
        return None

    def _activity_status(self, latest: str | None) -> str:
        if not latest:
            return "uncertain"
        try:
            date = datetime.fromisoformat(latest.replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        except ValueError:
            return "uncertain"
        return "active" if date >= datetime.now(timezone.utc) - timedelta(days=self.settings.active_days) else "inactive"
