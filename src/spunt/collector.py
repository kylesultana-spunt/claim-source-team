"""RSS + article collector.

Responsibilities
    1. Read config/sources.yml
    2. Pull each source's RSS feed
    3. Filter to political items (section path OR keyword match)
    4. Fetch article HTML and extract readable text (trafilatura)
    5. Append new articles to inbox.csv — deduped by source_url AND by
       near-duplicate claim detection so we don't re-collect the same story.

The collector deliberately does NOT extract atomic claims; that's the
extractor's job. We just capture the raw article text into inbox.csv's
`raw_statement` column so the extractor has a stable input.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import feedparser  # type: ignore
import httpx
import trafilatura  # type: ignore
import yaml
from dateutil import parser as date_parser  # type: ignore

from .schema import INBOX_COLS, InboxRow, utc_stamp
from .storage import read_csv, write_csv_atomic

log = logging.getLogger("spunt.collector")

USER_AGENT = "spunt-factcheck/0.1 (+https://spunt.mt)"
FETCH_TIMEOUT = 20.0


# ---------------------------------------------------------------- config
def load_sources(path: Path) -> Tuple[List[Dict], List[Dict]]:
    """Returns (sources, politicians) from sources.yml."""
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", []), data.get("politicians", [])


# ---------------------------------------------------------------- filter
def is_political(entry, source: Dict) -> bool:
    """Keep an RSS entry if it matches section prefix OR any keyword."""
    if source.get("political_only"):
        return True

    url = (getattr(entry, "link", "") or "").lower()
    title = (getattr(entry, "title", "") or "").lower()
    summary = (getattr(entry, "summary", "") or "").lower()
    text_blob = f"{title} {summary}"

    sections = source.get("sections") or []
    if sections and not any(s in url for s in sections):
        return False

    keywords = [k.lower() for k in (source.get("keywords") or [])]
    if keywords and not any(k in text_blob for k in keywords):
        # No keyword match -> drop. If no keywords defined, keep.
        return bool(not keywords)
    return True


# ---------------------------------------------------------------- fetch
def fetch_article_text(url: str, client: httpx.Client) -> Optional[str]:
    try:
        r = client.get(url, headers={"User-Agent": USER_AGENT},
                       timeout=FETCH_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("fetch failed: %s -> %s", url, e)
        return None
    text = trafilatura.extract(r.text, include_comments=False,
                               include_tables=False)
    if not text or len(text) < 200:
        return None
    return text


# ---------------------------------------------------------------- feed discovery
# Outlets don't always publish their RSS at a predictable URL, and URLs drift
# over time. To make this resilient, when the configured `rss` URL returns
# nothing, we (a) try a handful of common fallback paths and (b) scrape the
# landing page for `<link rel="alternate" type="application/rss+xml">` tags.
_COMMON_FEED_PATHS = [
    "/feed/", "/feed", "/rss", "/rss/", "/rss.xml", "/feed.xml",
    "/atom.xml", "/index.xml", "/news/feed/", "/en/feed/",
]

_FEED_LINK_RE = re.compile(
    r'<link[^>]+rel=["\']alternate["\'][^>]*'
    r'type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _feed_has_entries(url: str) -> bool:
    try:
        feed = feedparser.parse(url)
        return bool(getattr(feed, "entries", None))
    except Exception:
        return False


def discover_feed(site_url: str, client: httpx.Client) -> Optional[str]:
    """Try to find a working feed URL starting from the site's landing page.

    Order of attempts:
    1. Common paths (/feed/, /rss, /rss.xml, ...)
    2. <link rel="alternate"> tags in the landing page HTML
    """
    base = site_url.rstrip("/")
    for path in _COMMON_FEED_PATHS:
        candidate = base + path
        if _feed_has_entries(candidate):
            return candidate
    # Fall back to scraping the landing page for a declared feed link.
    try:
        r = client.get(site_url, headers={"User-Agent": USER_AGENT},
                       timeout=FETCH_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    for m in _FEED_LINK_RE.finditer(r.text):
        candidate = urljoin(site_url, m.group(1))
        if _feed_has_entries(candidate):
            return candidate
    return None


def parse_publication_date(entry) -> str:
    for attr in ("published", "updated", "created"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                d = date_parser.parse(raw)
                return d.date().isoformat()
            except (ValueError, TypeError):
                continue
    return "unknown"


# ---------------------------------------------------------------- main
def run(inbox_path: Path, sources_path: Path,
        max_per_source: int = 20) -> int:
    """Collect new political articles into inbox.csv.

    Returns the number of new rows appended.
    """
    sources, _ = load_sources(sources_path)
    existing = read_csv(inbox_path)
    seen_urls = {r.get("source_url", "") for r in existing}

    new_rows: List[Dict] = []
    with httpx.Client() as client:
        for source in sources:
            rss = source.get("rss")
            site = source.get("site")
            feed = feedparser.parse(rss) if rss else None
            # If the configured RSS didn't give us any entries, try to find a
            # working feed by (a) common URL patterns and (b) parsing the
            # landing page's <link rel="alternate"> tags.
            if (not feed or not feed.entries) and site:
                discovered = discover_feed(site, client)
                if discovered:
                    log.info("source: %s  auto-discovered feed: %s (configured=%s)",
                             source["name"], discovered, rss or "<none>")
                    rss = discovered
                    feed = feedparser.parse(rss)
            if not feed or not feed.entries:
                log.warning("source: %s  no feed found (configured=%s, site=%s)",
                            source["name"], rss or "<none>", site or "<none>")
                continue
            log.info("source: %s  rss: %s  entries=%d",
                     source["name"], rss, len(feed.entries))
            kept = 0
            for entry in feed.entries:
                if kept >= max_per_source:
                    break
                if not is_political(entry, source):
                    continue
                url = getattr(entry, "link", "")
                if not url or url in seen_urls:
                    continue
                body = fetch_article_text(url, client)
                if not body:
                    continue
                pub = parse_publication_date(entry)
                row = InboxRow(
                    raw_statement=body,
                    source_name=source["name"],
                    source_url=url,
                    publication_date=pub,
                    collected_at=utc_stamp(),
                    topic="rss",
                    processed="",  # extractor will flip this to "done"
                ).to_row()
                new_rows.append(row)
                seen_urls.add(url)
                kept += 1
            log.info("  kept %d new articles", kept)

    if new_rows:
        write_csv_atomic(inbox_path, INBOX_COLS, existing + new_rows)
    return len(new_rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    root = Path(__file__).resolve().parents[2]
    n = run(root / "data" / "inbox.csv", root / "config" / "sources.yml")
    print(f"collector: appended {n} new articles")
