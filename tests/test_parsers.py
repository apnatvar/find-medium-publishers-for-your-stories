from medium_tool.parsers import (
    parse_guidelines,
    parse_profile_story_links,
    parse_publication,
    parse_story,
)
from medium_tool.discovery import PublicationDiscovery


def test_profile_links_are_deduplicated_and_canonical(fixture_dir):
    html = (fixture_dir / "profile.html").read_text(encoding="utf-8")
    links = parse_profile_story_links(html)
    assert links == [
        "https://medium.com/@nattupi/why-we-seek-meaning-deadbeef1234",
        "https://medium.com/@nattupi/the-shape-of-habit-cafebabe5678",
    ]


def test_story_parser_uses_tags_as_primary_signal(fixture_dir):
    html = (fixture_dir / "story.html").read_text(encoding="utf-8")
    story = parse_story(html, "https://medium.com/story", "nattupi")
    assert story["title"] == "Why We Seek Meaning"
    assert story["tags"] == ["Psychology", "Philosophy"]
    assert story["is_self_published"] is True
    assert story["is_eligible"] is True


def test_publication_and_guideline_parsers(fixture_dir):
    publication = parse_publication(
        (fixture_dir / "publication.html").read_text(encoding="utf-8"),
        "https://medium.com/thought-garden",
    )
    rules = parse_guidelines((fixture_dir / "guidelines.html").read_text(encoding="utf-8"))
    assert publication["follower_count"] == 12_400
    assert publication["accepted_topics"] == ["philosophy", "psychology"]
    assert rules["accepts_published"] is True
    assert rules["requires_unpublished"] is False
    assert rules["onboarding_method"] == "medium-comment"


def test_guideline_url_is_reduced_to_publication_home():
    assert PublicationDiscovery._publication_url_from_guideline(
        "https://thought-garden.medium.com/submission-guidelines-abcdef123456"
    ) == "https://thought-garden.medium.com"
    assert PublicationDiscovery._publication_url_from_guideline(
        "https://medium.com/thought-garden/submission-guidelines-abcdef123456"
    ) == "https://medium.com/thought-garden"
    assert PublicationDiscovery._publication_url_from_guideline(
        "https://medium.com/@editor/submission-guidelines-abcdef123456"
    ) is None
