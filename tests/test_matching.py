from datetime import datetime, timezone

from medium_tool.matching import MatchingEngine


def test_strong_match_scores_above_threshold(settings, db):
    story_id = db.upsert_story({
        "title": "Why We Seek Meaning",
        "url": "https://medium.com/@nattupi/meaning-12345678",
        "tags": ["Philosophy", "Psychology", "Life"],
        "subtitle": "A reflection on human behavior",
        "is_self_published": True,
        "is_eligible": True,
    })
    pub_id = db.upsert_publication({
        "name": "Thought Garden",
        "url": "https://medium.com/thought-garden",
        "follower_count": 12_400,
        "accepted_topics": ["philosophy", "psychology", "life"],
        "accepts_published": True,
        "requires_unpublished": False,
        "accepts_submissions": True,
        "last_publication_date": datetime.now(timezone.utc).isoformat(),
        "latest_verification_date": datetime.now(timezone.utc).isoformat(),
        "status": "active",
    })
    result = MatchingEngine(settings, db).run()
    assert result[0]["score"] == 100
    assert result[0]["eligible"] is True


def test_unpublished_only_is_hard_rejection(settings, db):
    db.upsert_story({
        "title": "Published",
        "url": "https://medium.com/@nattupi/published-12345678",
        "tags": ["Philosophy"],
        "is_self_published": True,
        "is_eligible": True,
    })
    db.upsert_publication({
        "name": "Drafts Only",
        "url": "https://medium.com/drafts-only",
        "accepted_topics": ["philosophy"],
        "accepts_published": False,
        "requires_unpublished": True,
        "accepts_submissions": True,
        "status": "active",
    })
    result = MatchingEngine(settings, db).run()[0]
    assert result["eligible"] is False
    assert "Publication requires unpublished drafts" in result["rejection_reasons"]

