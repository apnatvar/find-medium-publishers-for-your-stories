import asyncio

import pytest

from medium_tool.matching import MatchingEngine
from medium_tool.workflows import WorkflowError, Workflows


def _seed(settings, db):
    db.upsert_story({
        "title": "Meaning", "url": "https://medium.com/@nattupi/meaning-12345678",
        "tags": ["Philosophy"], "is_self_published": True, "is_eligible": True,
    })
    db.upsert_publication({
        "name": "Garden", "url": "https://medium.com/garden",
        "accepted_topics": ["philosophy"], "follower_count": 1000,
        "accepts_published": True, "requires_unpublished": False,
        "accepts_submissions": True, "requires_application": False,
        "is_open_to_all": True, "status": "active",
    })
    MatchingEngine(settings, db).run()
    return db.one("SELECT id FROM matches")["id"]


def test_exact_approval_is_recorded(settings, db):
    match_id = _seed(settings, db)
    result = Workflows(settings, db).approve(match_id)
    assert result["status"] == "approved"
    assert db.one("SELECT status FROM submissions")["status"] == "approved"
    with pytest.raises(WorkflowError):
        Workflows(settings, db).approve(match_id)


def test_dry_run_blocks_submission(settings, db):
    match_id = _seed(settings, db)
    Workflows(settings, db).approve(match_id)
    with pytest.raises(WorkflowError, match="Dry-run"):
        asyncio.run(Workflows(settings, db).submit(match_id))
