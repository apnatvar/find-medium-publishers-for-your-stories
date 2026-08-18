from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone

from .config import Settings
from .db import Database, utcnow


SYNONYMS = {
    "self development": {"self improvement", "personal development", "growth"},
    "psychology": {"mental health", "mind", "human behavior", "human behaviour"},
    "philosophy": {"meaning", "wisdom", "life", "existentialism"},
    "personal experience": {"life lesson", "memoir", "personal essay", "personal thought"},
}

GENERIC_TOPICS = {"culture", "life", "personal", "self", "society", "writing"}


class MatchingEngine:
    def __init__(self, settings: Settings, db: Database):
        self.settings, self.db = settings, db
        self.pending_story_ids: set[int] = set()

    def run(self) -> list[dict]:
        stories = self.db.all("SELECT * FROM stories")
        publications = self.db.all("SELECT * FROM publications")
        self.pending_story_ids = {
            row["story_id"] for row in self.db.all(
                """SELECT DISTINCT story_id FROM submissions WHERE status IN
                   ('approved','submitted','application-required','application-sent')"""
            )
        }
        results = []
        with self.db.connect() as conn:
            for story in stories:
                for publication in publications:
                    result = self.score(dict(story), dict(publication))
                    now = utcnow()
                    conn.execute(
                        """INSERT INTO matches(
                            story_id,publication_id,score,eligible,explanation_json,
                            rejection_reasons_json,status,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(story_id,publication_id) DO UPDATE SET
                            score=excluded.score,eligible=excluded.eligible,
                            explanation_json=excluded.explanation_json,
                            rejection_reasons_json=excluded.rejection_reasons_json,
                            status=CASE WHEN matches.status IN ('approved','submitted')
                                THEN matches.status ELSE excluded.status END,
                            updated_at=excluded.updated_at""",
                        (
                            story["id"], publication["id"], result["score"], int(result["eligible"]),
                            json.dumps(result["explanation"]), json.dumps(result["rejection_reasons"]),
                            "recommended" if result["eligible"] else "skipped", now, now,
                        ),
                    )
                    results.append({"story_id": story["id"], "publication_id": publication["id"], **result})
            conn.execute(
                """WITH ranked AS (
                    SELECT id,ROW_NUMBER() OVER (
                        PARTITION BY story_id ORDER BY score DESC,publication_id
                    ) AS rank
                    FROM matches WHERE eligible=1
                )
                UPDATE matches SET status='skipped',updated_at=?
                WHERE id IN (SELECT id FROM ranked WHERE rank>3)
                  AND status NOT IN ('approved','submitted')""",
                (utcnow(),),
            )
        return results

    def score(self, story: dict, publication: dict) -> dict:
        story_tags = _terms(json.loads(story["tags_json"] or "[]"))
        accepted = _terms(json.loads(publication["accepted_topics_json"] or "[]"))
        excluded = _terms(json.loads(publication["excluded_topics_json"] or "[]"))
        expanded_story = _expand(story_tags)
        direct_matches = (story_tags & accepted) - GENERIC_TOPICS
        expanded_matches = (expanded_story & _expand(accepted)) - GENERIC_TOPICS
        if direct_matches:
            tag_score = 40.0
        elif expanded_matches:
            tag_score = 34.0
        else:
            tag_score = round(25.0 * _token_similarity(story_tags, accepted), 1)

        context = _terms([story.get("title") or "", story.get("subtitle") or ""])
        semantic_pool = _expand(story_tags | context)
        publication_context = _terms([
            publication.get("description") or "", *json.loads(publication["accepted_topics_json"] or "[]")
        ])
        semantic_similarity = _token_similarity(semantic_pool, _expand(publication_context))
        if expanded_matches:
            semantic_similarity = max(semantic_similarity, 1.0)
        semantic_score = min(25.0, semantic_similarity * 25)

        accepts = publication.get("accepts_published")
        acceptance_score = 20.0 if accepts == 1 else (12.0 if accepts is None else 0.0)
        activity_score = 10.0 if publication["status"] == "active" else 0.0
        followers = publication.get("follower_count")
        if followers is None:
            size_score = 3.0
        elif self.settings.min_followers <= followers <= self.settings.max_followers:
            size_score = 5.0
        else:
            distance = min(
                abs(math.log10(max(followers, 1)) - math.log10(self.settings.min_followers)),
                abs(math.log10(max(followers, 1)) - math.log10(self.settings.max_followers)),
            )
            size_score = max(0.0, 5.0 - 2.5 * distance)

        reasons = self._hard_rejections(story, publication, expanded_story, excluded)
        total = round(tag_score + semantic_score + acceptance_score + activity_score + size_score, 1)
        eligible = not reasons and total >= self.settings.min_score
        if not reasons and total < self.settings.min_score:
            reasons.append(f"Score {total:.1f} is below configured minimum {self.settings.min_score:.1f}")
        explanation = {
            "tag_overlap": {"points": round(tag_score, 1), "matched": sorted(expanded_matches)},
            "semantic_topic_fit": {
                "points": round(semantic_score, 1),
                "matched": sorted(_tokens(semantic_pool) & _tokens(_expand(publication_context))),
            },
            "published_story_acceptance": {"points": acceptance_score, "value": _tri_text(accepts)},
            "recent_activity": {"points": activity_score, "status": publication["status"]},
            "size_preference": {"points": round(size_score, 1), "followers": followers},
        }
        return {"score": total, "eligible": eligible, "explanation": explanation, "rejection_reasons": reasons}

    def _hard_rejections(self, story: dict, publication: dict, topics: set[str], excluded: set[str]) -> list[str]:
        reasons = []
        if publication["status"] == "inactive":
            reasons.append("Publication is inactive")
        if topics & excluded:
            reasons.append("Guidelines explicitly exclude: " + ", ".join(sorted(topics & excluded)))
        if story["is_self_published"] and publication.get("requires_unpublished") == 1:
            reasons.append("Publication requires unpublished drafts")
        if story.get("current_publication") and story["current_publication"].lower() == publication["name"].lower():
            reasons.append("Story is already in this publication")
        if publication.get("accepts_submissions") == 0:
            reasons.append("Publication does not currently accept submissions")
        if story["id"] in self.pending_story_ids:
            reasons.append("Another submission of this story is pending")
        return reasons


def _terms(values: list[str] | set[str]) -> set[str]:
    result = set()
    for value in values:
        normalized = re.sub(r"[^a-z0-9 ]", " ", value.lower()).strip()
        if normalized:
            normalized = re.sub(r"\s+", " ", normalized)
            if normalized.endswith("ies"):
                normalized = normalized[:-3] + "y"
            elif normalized.endswith("s") and not normalized.endswith("ss"):
                normalized = normalized[:-1]
            result.add(normalized)
    return result


def _expand(terms: set[str]) -> set[str]:
    expanded = set(terms)
    for key, values in SYNONYMS.items():
        group = {key, *values}
        if group & terms:
            expanded |= group
    return expanded


def _tri_text(value) -> str:
    return "yes" if value == 1 else ("no" if value == 0 else "unknown")


def _tokens(terms: set[str]) -> set[str]:
    stop = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "with"}
    return {
        token for term in terms for token in term.split()
        if len(token) > 2 and token not in stop
    }


def _token_similarity(left: set[str], right: set[str]) -> float:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    return (2 * overlap) / (len(left_tokens) + len(right_tokens))
