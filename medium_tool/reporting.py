from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .db import Database


class Reporter:
    def __init__(self, settings: Settings, db: Database):
        self.settings, self.db = settings, db

    def status(self) -> dict:
        counts = {}
        for table in ("stories", "publications", "matches", "writer_applications", "submissions", "errors"):
            counts[table] = self.db.one(f"SELECT COUNT(*) n FROM {table}")["n"]
        counts["active_publications"] = self.db.one(
            "SELECT COUNT(*) n FROM publications WHERE status='active'"
        )["n"]
        counts["recommended_matches"] = self.db.one(
            "SELECT COUNT(*) n FROM matches WHERE eligible=1 AND status='recommended'"
        )["n"]
        counts["dry_run"] = self.settings.dry_run
        return counts

    def generate(self, path: Path | None = None) -> Path:
        path = path or self.settings.artifact_dir / "dry-run-report.md"
        stories = self.db.all("SELECT * FROM stories ORDER BY publication_date DESC,title")
        publications = self.db.all("SELECT * FROM publications WHERE status='active' ORDER BY name")
        applications = self.db.all(
            """SELECT p.name,p.onboarding_method,p.onboarding_destination,p.guideline_url
               FROM publications p WHERE p.requires_application=1 AND p.status='active'
               ORDER BY p.name"""
        )
        lines = [
            "# Medium publication assistant — dry-run report",
            "",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            f"Profile: {self.settings.profile_url}",
            "",
            "## Imported stories",
            "",
        ]
        if not stories:
            lines.append("_No stories imported._")
        for story in stories:
            tags = ", ".join(json.loads(story["tags_json"])) or "none detected"
            lines += [f"### [{story['title']}]({story['url']})", "", f"- Tags: {tags}"]
            matches = self.db.all(
                """SELECT m.*,p.name,p.url,p.follower_count,p.status AS publication_status
                   FROM matches m JOIN publications p ON p.id=m.publication_id
                   WHERE m.story_id=? ORDER BY m.eligible DESC,m.score DESC LIMIT 3""",
                (story["id"],),
            )
            eligible = [m for m in matches if m["eligible"]]
            if eligible:
                lines.append("- Top eligible publications:")
                for match in eligible:
                    lines.append(
                        f"  - [{match['name']}]({match['url']}): {match['score']:.1f}/100"
                    )
            else:
                lines.append("- Reliable match: none found")
            rejected = self.db.all(
                """SELECT m.score,m.rejection_reasons_json,p.name FROM matches m
                   JOIN publications p ON p.id=m.publication_id
                   WHERE m.story_id=? AND m.eligible=0 ORDER BY m.score DESC""",
                (story["id"],),
            )
            if rejected:
                lines.append("- Rejected matches:")
                for match in rejected:
                    reasons = "; ".join(json.loads(match["rejection_reasons_json"]))
                    lines.append(f"  - {match['name']} ({match['score']:.1f}): {reasons}")
            lines.append("")
        lines += ["## Active publication candidates", ""]
        if not publications:
            lines.append("_No reliably active publications found._")
        for pub in publications:
            followers = pub["follower_count"] if pub["follower_count"] is not None else "unknown"
            topics = ", ".join(json.loads(pub["accepted_topics_json"])) or "not reliably extracted"
            lines.append(f"- [{pub['name']}]({pub['url']}) — {followers} followers; topics: {topics}")
        lines += ["", "## Publications requiring writer applications", ""]
        if not applications:
            lines.append("_None identified._")
        for app in applications:
            destination = app["onboarding_destination"] or app["guideline_url"] or "unknown"
            lines.append(f"- {app['name']}: {app['onboarding_method'] or 'unknown'} — {destination}")
        lines += [
            "", "## Safety summary", "",
            f"- Dry-run mode: {'enabled' if self.settings.dry_run else 'disabled'}",
            f"- Minimum score: {self.settings.min_score}",
            f"- Activity window: {self.settings.active_days} days",
            f"- Preferred follower range: {self.settings.min_followers:,}–{self.settings.max_followers:,}",
            "- No application or submission was performed by this report.",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
