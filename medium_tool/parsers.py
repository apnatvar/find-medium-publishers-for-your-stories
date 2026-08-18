from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


TOPIC_WORDS = {
    "philosophy", "psychology", "life", "self", "self improvement",
    "personal development", "mental health", "relationships", "human behavior",
    "human behaviour", "mindfulness", "productivity", "creativity", "writing",
}


def canonical_medium_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl().rstrip("/")


def parse_profile_story_links(html: str, base_url: str = "https://medium.com") -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if not href or "/tag/" in href or href.startswith("/@") and href.count("/") == 1:
            continue
        full = canonical_medium_url(urljoin(base_url, href))
        if "medium.com" not in urlparse(full).netloc:
            continue
        if re.search(r"/[^/]+-[a-f0-9]{8,}$", urlparse(full).path, re.I):
            urls.append(full)
    return list(dict.fromkeys(urls))


def parse_story(html: str, url: str, profile_handle: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = _meta(soup, "property", "og:title") or _text(soup.select_one("h1")) or "Untitled"
    subtitle = _meta(soup, "property", "og:description") or _meta(soup, "name", "description")
    published = _meta(soup, "property", "article:published_time")
    if not published:
        time = soup.select_one("time[datetime]")
        published = time.get("datetime") if time else None
    tags = [_text(x) for x in soup.select('a[href*="/tag/"]')]
    for meta in soup.select('meta[property="article:tag"], meta[name="news_keywords"]'):
        tags.extend(re.split(r",\s*", meta.get("content", "")))
    tags = list(dict.fromkeys(t.strip() for t in tags if t.strip()))
    canonical = _meta(soup, "property", "og:url") or url
    author_link = soup.select_one(f'a[href*="/@{profile_handle.lstrip("@")}"]')
    publication = _publication_name(soup, profile_handle)
    is_self = not publication
    return {
        "title": title.strip(),
        "url": canonical_medium_url(canonical),
        "publication_date": published,
        "tags": tags,
        "subtitle": subtitle.strip() if subtitle else None,
        "current_publication": publication,
        "is_self_published": is_self,
        "is_eligible": bool(author_link or profile_handle.lower() in html.lower()) and is_self,
        "eligibility_reason": "Self-published story" if is_self else f"Already in {publication}",
        "processing_status": "discovered",
    }


def parse_publication(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    name = _meta(soup, "property", "og:title") or _text(soup.select_one("h1")) or urlparse(url).path.strip("/")
    description = _meta(soup, "property", "og:description") or _meta(soup, "name", "description")
    followers = None
    match = re.search(r"([\d,.]+)\s*([KkMm])?\s+followers?", text)
    if match:
        followers = _number(match.group(1), match.group(2))
    dates = []
    for node in soup.select("time[datetime]"):
        raw = node.get("datetime", "")
        try:
            dates.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            pass
    for month, day, year in re.findall(
        r"(?:last published\s+)?(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2})(?:,\s*(\d{4}))?",
        text,
        re.I,
    ):
        try:
            inferred_year = int(year) if year else datetime.now().year
            parsed_date = datetime.strptime(
                f"{month} {day} {inferred_year}", "%b %d %Y"
            ).replace(tzinfo=timezone.utc)
            if not year and parsed_date > datetime.now(timezone.utc) + timedelta(days=7):
                parsed_date = parsed_date.replace(year=parsed_date.year - 1)
            dates.append(parsed_date)
        except ValueError:
            pass
    topics = []
    for anchor in soup.select('a[href*="/tag/"]'):
        value = _text(anchor).lower()
        if value:
            topics.append(value)
    unique_dates = {date.date(): date for date in dates}
    recent_cutoff = datetime.now(timezone.utc).date().toordinal() - 30
    return {
        "name": re.sub(r"\s*[|–-]\s*Medium\s*$", "", name).strip(),
        "url": canonical_medium_url(url),
        "description": description,
        "follower_count": followers,
        "accepted_topics": list(dict.fromkeys(topics)),
        "last_publication_date": max(unique_dates.values()).isoformat() if unique_dates else None,
        "posts_last_30_days": sum(1 for date in unique_dates if date.toordinal() >= recent_cutoff) if unique_dates else None,
    }


def parse_guidelines(text_or_html: str) -> dict:
    soup = BeautifulSoup(text_or_html, "html.parser")
    text = soup.get_text(" ", strip=True) if soup.find() else text_or_html
    lower = re.sub(r"\s+", " ", text.lower())
    unpublished_only = bool(re.search(r"(only|must be).{0,35}(unpublished|draft)|no previously published", lower))
    accepts_published = bool(re.search(r"(accept|welcome).{0,35}(previously published|already published)", lower))
    closed = bool(re.search(r"(not|no longer|currently.?t).{0,20}(accepting|open).{0,20}(submission|writer)", lower))
    method = "other"
    destination = None
    if re.search(r"open (submission|to all)|submit to (this|the) publication", lower):
        method = "medium-open-submission"
    elif re.search(r"comment.{0,60}(add|writer)", lower):
        method = "medium-comment"
    elif (mailto := _action_link(soup, 'a[href^="mailto:"]')):
        method, destination = "email", mailto.get("href", "")[7:]
    elif (form := _action_link(
        soup, 'a[href*="forms.gle"], a[href*="docs.google.com/forms"], a[href*="typeform"]'
    )):
        method, destination = "external-form", form.get("href")
    elif re.search(r"(request|ask).{0,35}(add|become).{0,20}writer", lower):
        method = "request-writer-access"
    topics = sorted({word for word in TOPIC_WORDS if word in lower})
    return {
        "accepts_published": True if accepts_published else (False if unpublished_only else None),
        "requires_unpublished": unpublished_only,
        "accepts_submissions": not closed,
        "onboarding_method": method,
        "onboarding_destination": destination,
        "requires_application": method not in {"medium-open-submission"},
        "is_open_to_all": method == "medium-open-submission",
        "accepted_topics": topics,
        "raw_text": text,
    }


def _meta(soup: BeautifulSoup, attr: str, value: str) -> str | None:
    node = soup.find("meta", attrs={attr: value})
    return node.get("content") if node else None


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _number(number: str, suffix: str | None) -> int:
    value = float(number.replace(",", ""))
    if suffix:
        value *= {"k": 1_000, "m": 1_000_000}[suffix.lower()]
    return int(value)


def _publication_name(soup: BeautifulSoup, profile_handle: str) -> str | None:
    candidate = soup.select_one('meta[property="article:section"]')
    if candidate and candidate.get("content"):
        return candidate["content"]
    json_nodes = soup.select('script[type="application/ld+json"]')
    for node in json_nodes:
        try:
            data = json.loads(node.string or "{}")
        except json.JSONDecodeError:
            continue
        publisher = data.get("publisher") if isinstance(data, dict) else None
        if isinstance(publisher, dict):
            name = publisher.get("name")
            if name and name.lower() not in {"medium", profile_handle.lstrip("@").lower()}:
                return name
    return None


def _action_link(soup: BeautifulSoup, selector: str):
    for link in soup.select(selector):
        context = " ".join(
            parent.get_text(" ", strip=True)
            for parent in [link.parent, link.parent.parent if link.parent else None]
            if parent
        ).lower()
        if re.search(r"(submit|submission|writer|application|send.{0,20}(story|draft|pitch))", context):
            return link
    return None
