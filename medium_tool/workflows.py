from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from .browser import BrowserSession, ManualIntervention
from .config import Settings
from .db import Database, utcnow
from .parsers import parse_guidelines


class WorkflowError(RuntimeError):
    pass


class Workflows:
    def __init__(self, settings: Settings, db: Database, browser: BrowserSession | None = None):
        self.settings, self.db, self.browser = settings, db, browser

    def review(self) -> list[dict]:
        return [
            dict(row) for row in self.db.all(
                """SELECT m.id AS match_id,m.score,m.status,m.explanation_json,
                          s.title AS story,s.url AS story_url,p.name AS publication,
                          p.follower_count,p.status AS activity,p.accepts_published,
                          p.requires_application,p.onboarding_method,p.guideline_url
                   FROM matches m JOIN stories s ON s.id=m.story_id
                   JOIN publications p ON p.id=m.publication_id
                   WHERE m.eligible=1 AND m.status='recommended'
                   ORDER BY m.score DESC,s.title,p.name"""
            )
        ]

    def approve(self, match_id: int) -> dict:
        match = self._match(match_id)
        if not match["eligible"]:
            raise WorkflowError("This match is not eligible and cannot be approved.")
        pending = self.db.one(
            """SELECT id FROM submissions WHERE story_id=? AND status IN
               ('approved','submitted','application-required','application-sent')""",
            (match["story_id"],),
        )
        if pending:
            raise WorkflowError("This story already has a pending or approved submission.")
        now = utcnow()
        with self.db.connect() as conn:
            conn.execute("INSERT INTO approvals(match_id,action,created_at) VALUES(?,?,?)", (match_id, "approve", now))
            conn.execute("UPDATE matches SET status='approved',updated_at=? WHERE id=?", (now, match_id))
            status = "application-required" if match["requires_application"] and not match["is_open_to_all"] else "approved"
            conn.execute(
                """INSERT INTO submissions(match_id,story_id,publication_id,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (match_id, match["story_id"], match["publication_id"], status, now, now),
            )
        return {"match_id": match_id, "status": status}

    def reject(self, match_id: int) -> dict:
        self._match(match_id)
        now = utcnow()
        with self.db.connect() as conn:
            conn.execute("INSERT INTO approvals(match_id,action,created_at) VALUES(?,?,?)", (match_id, "reject", now))
            conn.execute("UPDATE matches SET status='rejected',updated_at=? WHERE id=?", (now, match_id))
        return {"match_id": match_id, "status": "rejected"}

    def prepare_application(self, publication_id: int) -> dict:
        publication = self.db.one("SELECT * FROM publications WHERE id=?", (publication_id,))
        if not publication:
            raise WorkflowError(f"Publication {publication_id} was not found.")
        publication = dict(publication)
        if publication["accepts_submissions"] == 0:
            raise WorkflowError("The publication is not currently accepting submissions.")
        stories = self.db.all(
            """SELECT DISTINCT s.id,s.title,s.url,s.tags_json,m.score
               FROM matches m JOIN stories s ON s.id=m.story_id
               WHERE m.publication_id=? AND m.eligible=1
               ORDER BY m.score DESC LIMIT 3""",
            (publication_id,),
        )
        if not stories:
            raise WorkflowError("No eligible stories are available for this application.")
        tags: list[str] = []
        for story in stories:
            tags.extend(json.loads(story["tags_json"]))
        topics = ", ".join(list(dict.fromkeys(tags))[:6])
        links = "\n".join(f"- {s['title']}: {s['url']}" for s in stories)
        message = (
            f"Hello {publication['name']} editors,\n\n"
            f"I write about {topics}. My Medium profile is {self.settings.profile_url}.\n\n"
            f"Here are a few relevant stories:\n{links}\n\n"
            "Would you please add me as a writer if my work is a fit for your publication?\n\nThank you."
        )
        now = utcnow()
        existing = self.db.one(
            """SELECT id FROM writer_applications WHERE publication_id=? AND status IN
               ('application-required','approved','application-sent')""",
            (publication_id,),
        )
        if existing:
            app_id = existing["id"]
            self.db.execute(
                """UPDATE writer_applications SET destination=?,method=?,message=?,
                   referenced_story_ids_json=?,updated_at=? WHERE id=?""",
                (
                    publication["onboarding_destination"] or publication["guideline_url"],
                    publication["onboarding_method"] or "other", message,
                    json.dumps([s["id"] for s in stories]), now, app_id,
                ),
            )
        else:
            app_id = self.db.execute(
                """INSERT INTO writer_applications(
                    publication_id,destination,method,message,referenced_story_ids_json,
                    status,created_at,updated_at
                ) VALUES(?,?,?,?,?,'application-required',?,?)""",
                (
                    publication_id, publication["onboarding_destination"] or publication["guideline_url"],
                    publication["onboarding_method"] or "other", message,
                    json.dumps([s["id"] for s in stories]), now, now,
                ),
            )
        return {
            "application_id": app_id, "publication": publication["name"],
            "method": publication["onboarding_method"] or "other",
            "destination": publication["onboarding_destination"] or publication["guideline_url"],
            "message": message, "stories": [dict(s) for s in stories],
            "status": "application-required",
        }

    async def send_application(self, application_id: int) -> dict:
        if self.settings.dry_run:
            raise WorkflowError("Dry-run mode prevents sending applications.")
        if not self.browser:
            raise WorkflowError("A browser session is required.")
        app = self.db.one(
            """SELECT a.*,p.name FROM writer_applications a
               JOIN publications p ON p.id=a.publication_id WHERE a.id=?""",
            (application_id,),
        )
        if not app or app["status"] != "approved":
            raise WorkflowError("Application must be explicitly approved before it can be sent.")
        method = app["method"]
        if not app["destination"]:
            raise ManualIntervention("No reliable application destination was identified.")
        if method in {"email", "external-form", "other"}:
            raise ManualIntervention(
                f"{method} application requires manual completion at {app['destination']}."
            )
        page = await self.browser.page(app["destination"], "send-writer-application")
        try:
            if method == "medium-comment":
                editor = page.locator('[contenteditable="true"]').last
                if await editor.count() != 1:
                    raise ManualIntervention("Could not identify a single Medium comment editor.")
                await editor.fill(app["message"])
                button = page.get_by_role("button", name="Publish")
                if await button.count() != 1:
                    raise ManualIntervention("Could not identify the comment Publish button.")
                await button.click()
            else:
                raise ManualIntervention("Writer-access workflow needs manual completion.")
            now = utcnow()
            self.db.execute(
                "UPDATE writer_applications SET status='application-sent',sent_at=?,updated_at=? WHERE id=?",
                (now, now, application_id),
            )
            self.browser.audit(
                "send-application", "success", target_type="publication",
                target_id=app["publication_id"], destination=app["destination"],
                details={"application_id": application_id},
            )
            return {"application_id": application_id, "status": "application-sent"}
        finally:
            await page.close()

    async def submit(self, match_id: int) -> dict:
        if self.settings.dry_run:
            raise WorkflowError("Dry-run mode prevents submissions.")
        if not self.browser:
            raise WorkflowError("A browser session is required.")
        match = self._match(match_id)
        submission = self.db.one("SELECT * FROM submissions WHERE match_id=? ORDER BY id DESC LIMIT 1", (match_id,))
        if not submission or submission["status"] != "approved":
            raise WorkflowError("Exact match approval is required before submission.")
        self._check_limits()
        if match["status"] == "inactive" or match["accepts_submissions"] == 0:
            raise WorkflowError("Publication is not currently eligible for submission.")
        if match["requires_unpublished"] and match["is_self_published"]:
            raise WorkflowError("Published story conflicts with unpublished-draft requirement.")
        if match["is_self_published"] and match["accepts_published"] != 1:
            raise ManualIntervention(
                "The guidelines do not explicitly confirm acceptance of previously published stories."
            )
        guideline_hash = await self._revalidate_guidelines(match)
        await self._submit_via_medium(match)
        now = utcnow()
        self.db.execute(
            """UPDATE submissions SET status='submitted',submitted_at=?,guideline_hash=?,
               updated_at=? WHERE id=?""",
            (now, guideline_hash, now, submission["id"]),
        )
        self.db.execute("UPDATE matches SET status='submitted',updated_at=? WHERE id=?", (now, match_id))
        return {"match_id": match_id, "status": "submitted"}

    async def _revalidate_guidelines(self, match: dict) -> str:
        if not match["guideline_url"]:
            raise ManualIntervention("No guideline URL is available for immediate revalidation.")
        page = await self.browser.page(match["guideline_url"], "pre-submit-guideline-check")
        try:
            rules = parse_guidelines(await page.content())
            digest = hashlib.sha256(rules["raw_text"].encode("utf-8")).hexdigest()
            latest = self.db.one(
                "SELECT content_hash FROM guidelines WHERE publication_id=? ORDER BY verified_at DESC LIMIT 1",
                (match["publication_id"],),
            )
            if not latest or latest["content_hash"] != digest:
                raise ManualIntervention("Publication guidelines changed; review and rematch before submitting.")
            if not rules["accepts_submissions"]:
                raise WorkflowError("Revalidated guidelines say submissions are closed.")
            if rules["requires_unpublished"] and match["is_self_published"]:
                raise WorkflowError("Revalidated guidelines require an unpublished draft.")
            return digest
        finally:
            await page.close()

    async def _submit_via_medium(self, match: dict) -> None:
        page = await self.browser.page(match["story_url"], "submit-story")
        try:
            more = page.locator('button[data-testid="headerStoryOptionsButton"]')
            if await more.count() != 1:
                more = page.get_by_role("button", name=re.compile(r"(more options|toggle actions menu)", re.I))
            if await more.count() < 1:
                raise ManualIntervention("Could not find the story actions menu.")
            await more.first.click()
            edit = page.get_by_text("Edit story", exact=True)
            if await edit.count() != 1:
                raise ManualIntervention("Could not find an unambiguous Edit story action.")
            await edit.click()
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(750)
            menu = page.locator("button.js-moreActionsButton")
            if await menu.count() != 1:
                menu = page.get_by_role("button", name=re.compile(r"(more options|toggle actions menu)", re.I))
            if await menu.count() < 1:
                raise ManualIntervention("Could not find the editor actions menu.")
            await menu.first.click()
            add = page.get_by_text(re.compile(r"(submit|add) to publication", re.I))
            if await add.count() != 1:
                raise ManualIntervention("Submit to publication is unavailable or ambiguous.")
            await add.click()
            option = page.get_by_text(match["publication_name"], exact=True)
            if await option.count() != 1:
                raise ManualIntervention("Publication is not available in the story submission dialog.")
            await option.click()
            submit = page.get_by_role("button", name=re.compile(r"(send for review|submit)", re.I))
            if await submit.count() != 1:
                raise ManualIntervention("Final Submit button is unavailable or ambiguous.")
            await submit.click()
            await page.wait_for_timeout(1500)
            await self.browser.ensure_safe(page, "submit-story-confirmation")
            if await page.get_by_role("dialog").count():
                raise ManualIntervention("Medium displayed an unexpected confirmation dialog.")
            body = await page.locator("body").inner_text()
            if not re.search(r"(sent for review|submitted to|submission.{0,30}(sent|pending))", body, re.I):
                raise ManualIntervention(
                    "The submission result could not be verified; inspect Medium and the saved audit state."
                )
            self.browser.audit(
                "submit-story", "success", target_type="match", target_id=match["id"],
                destination=match["publication_url"],
            )
        except Exception as exc:
            await self.browser.capture_failure(page, "submit-story", exc)
            raise
        finally:
            await page.close()

    def _check_limits(self) -> None:
        now = datetime.now(timezone.utc)
        day = (now - timedelta(days=1)).isoformat()
        week = (now - timedelta(days=7)).isoformat()
        daily = self.db.one("SELECT COUNT(*) n FROM submissions WHERE submitted_at>=?", (day,))["n"]
        weekly = self.db.one("SELECT COUNT(*) n FROM submissions WHERE submitted_at>=?", (week,))["n"]
        if daily >= self.settings.daily_limit:
            raise WorkflowError("Configured daily submission limit has been reached.")
        if weekly >= self.settings.weekly_limit:
            raise WorkflowError("Configured weekly submission limit has been reached.")

    def _match(self, match_id: int) -> dict:
        row = self.db.one(
            """SELECT m.*,s.title AS story_title,s.url AS story_url,s.is_self_published,
                      s.current_publication,p.name AS publication_name,p.url AS publication_url,
                      p.status,p.accepts_submissions,p.accepts_published,p.requires_unpublished,
                      p.requires_application,p.is_open_to_all,p.guideline_url
               FROM matches m JOIN stories s ON s.id=m.story_id
               JOIN publications p ON p.id=m.publication_id WHERE m.id=?""",
            (match_id,),
        )
        if not row:
            raise WorkflowError(f"Match {match_id} was not found.")
        return dict(row)
