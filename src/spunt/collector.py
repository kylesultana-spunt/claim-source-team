"""Article collector — three discovery paths, one inbox.

Responsibilities
    1. Read config/sources.yml
    2. For each source, try three ways of getting recent article URLs:
       a. The site's own RSS feed (configured or auto-discovered).
       b. Google News RSS aggregation (site: query against news.google.com).
       c. News sitemap (/sitemap-news.xml etc.).
       URLs are merged and deduped so we hit each article once.
    3. Filter to political items (section path OR keyword match).
    4. Fetch article HTML and extract readable text (trafilatura).
    5. Append new articles to inbox.csv — deduped by source_url.

The collector deliberately does NOT extract atomic claims; that's the
extractor's job. We just capture the raw article text into inbox.csv's
`raw_statement` column so the extractor has a stable input.

Why three paths?
    Maltese news sites increasingly sit behind Cloudflare bot-challenge,
    which 403s simple HTTP clients. Google News is never blocked against
    itself, and news sitemaps are usually less protected than RSS. Having
    three independent paths means a blocked feed from one outlet doesn't
    mean zero coverage from that outlet.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

import feedparser  # type: ignore
import httpx
import trafilatura  # type: ignore
import yaml
from dateutil import parser as date_parser  # type: ignore

from .schema import INBOX_COLS, InboxRow, utc_stamp
from .storage import read_csv, write_csv_atomic

log = logging.getLogger("spunt.collector")

FETCH_TIMEOUT = 20.0

# A browser-like User-Agent + Accept headers. The old UA ("spunt-factcheck/0.1")
# was getting 403'd by Cloudflare on several Maltese news sites because it
# obviously looks like a bot. A realistic Chrome UA bypasses the easiest
# layer of bot protection — not a silver bullet against full JS challenges,
# but enough for most RSS/sitemap endpoints.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/rss+xml,application/atom+xml;q=0.8,*/*;q=0.7"
    ),
    "Accept-Language": "en-GB,en;q=0.9,mt;q=0.8",
}


# ---------------------------------------------------------------- config
def load_sources(path: Path) -> Tuple[List[Dict], List[Dict]]:
    """Returns (sources, politicians) from sources.yml."""
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", []), data.get("politicians", [])


# ---------------------------------------------------------------- filter
def is_political(entry, source: Dict) -> bool:
    """Keep an entry if it matches section prefix OR any keyword.

    `entry` can be a feedparser entry OR our SimpleNamespace from sitemap
    parsing — both expose `.link`, `.title`, `.summary`.
    """
    if source.get("political_only"):
        return True

    url = (getattr(entry, "link", "") or "").lower()
    title = (getattr(entry, "title", "") or "").lower()
    summary = (getattr(entry, "summary", "") or "").lower()
    text_blob = f"{title} {summary}"

    sections = source.get("sections") or []
    # Sitemaps and Google News often give URLs on the same domain but without
    # the section prefix in the configured list (Google News strips them; some
    # sitemaps include the home page). If sections are configured but text
    # matches a keyword, still keep it.
    section_ok = not sections or any(s in url for s in sections)

    keywords = [k.lower() for k in (source.get("keywords") or [])]
    keyword_ok = not keywords or any(k in text_blob for k in keywords)

    if sections and keywords:
        # Either signal is enough — keyword in title OR section in URL.
        return section_ok or keyword_ok
    return section_ok and keyword_ok


# ---------------------------------------------------------------- fetch article body
def fetch_article_text(url: str, client: httpx.Client) -> Optional[str]:
    try:
        r = client.get(url, headers=BROWSER_HEADERS,
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


# ================================================================
# PATH 1 — original RSS feed (configured or auto-discovered)
# ================================================================
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
        feed = feedparser.parse(url, request_headers=BROWSER_HEADERS)
        return bool(getattr(feed, "entries", None))
    except Exception:
        return False


def discover_feed(site_url: str, client: httpx.Client) -> Optional[str]:
    """Try to find a working feed URL starting from the site's landing page."""
    base = site_url.rstrip("/")
    for path in _COMMON_FEED_PATHS:
        candidate = base + path
        if _feed_has_entries(candidate):
            return candidate
    try:
        r = client.get(site_url, headers=BROWSER_HEADERS,
                       timeout=FETCH_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    for m in _FEED_LINK_RE.finditer(r.text):
        candidate = urljoin(site_url, m.group(1))
        if _feed_has_entries(candidate):
            return candidate
    return None


def collect_from_rss(source: Dict, client: httpx.Client) -> List:
    """Return feedparser-style entries from the source's own RSS feed."""
    rss = source.get("rss")
    site = source.get("site")
    feed = feedparser.parse(rss, request_headers=BROWSER_HEADERS) if rss else None
    if (not feed or not feed.entries) and site:
        discovered = discover_feed(site, client)
        if discovered:
            log.info("  [rss] auto-discovered %s", discovered)
            feed = feedparser.parse(discovered, request_headers=BROWSER_HEADERS)
    if not feed or not feed.entries:
        return []
    return list(feed.entries)


# ================================================================
# PATH 2 — Google News RSS (site: query)
# ================================================================
def google_news_feed_url(site_url: str, when: str = "2d") -> str:
    """Build a Google News RSS query URL for this site.

    `when:2d` restricts results to the last ~2 days, keeping the pipeline
    focused on recent news. Malta (MT) locale + English.
    """
    host = (urlparse(site_url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    q = quote(f"site:{host} when:{when}")
    return (f"https://news.google.com/rss/search?q={q}"
            f"&hl=en-MT&gl=MT&ceid=MT:en")


def _resolve_google_news_url(link: str, client: httpx.Client) -> Optional[str]:
    """Resolve a Google News wrapper URL to the real article URL.

    Google News RSS links look like `https://news.google.com/rss/articles/CBMi...`
    and use a client-side redirect, so httpx's automatic redirect-following
    doesn't get us to the destination. We fetch the wrapper page and look
    for the real URL in its HTML (data-n-au, og:url, canonical).

    Returns None if we can't find a non-news.google.com URL — the caller
    should skip that entry rather than store the Google wrapper URL.
    """
    if "news.google.com" not in link:
        return link
    try:
        r = client.get(link, headers=BROWSER_HEADERS,
                       timeout=FETCH_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError:
        return None
    # If redirects already walked us off Google, great.
    final = str(r.url)
    if "news.google.com" not in final:
        return final
    # Otherwise scrape the wrapper HTML for the real URL.
    for pattern in (
        r'data-n-au=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
    ):
        m = re.search(pattern, r.text, re.IGNORECASE)
        if m:
            candidate = m.group(1)
            if "news.google.com" not in candidate:
                return candidate
    return None


def collect_from_google_news(source: Dict, client: httpx.Client) -> List:
    """Return entries (with real URLs) from Google News for this site."""
    site = source.get("site")
    if not site:
        return []
    gn_url = google_news_feed_url(site)
    try:
        feed = feedparser.parse(gn_url, request_headers=BROWSER_HEADERS)
    except Exception as e:
        log.warning("  [gnews] parse failed: %s", e)
        return []
    if not getattr(feed, "entries", None):
        return []
    resolved: List = []
    host = (urlparse(site).hostname or "").lower().lstrip("www.")
    for e in feed.entries:
        raw_link = getattr(e, "link", "") or ""
        real = _resolve_google_news_url(raw_link, client)
        if not real:
            continue
        # Only keep results actually on the target domain — Google News's
        # `site:` operator is fuzzy and occasionally includes other outlets.
        real_host = (urlparse(real).hostname or "").lower().lstrip("www.")
        if host and host not in real_host:
            continue
        resolved.append(SimpleNamespace(
            link=real,
            title=getattr(e, "title", "") or "",
            summary=getattr(e, "summary", "") or "",
            published=getattr(e, "published", "") or "",
        ))
    return resolved


# ================================================================
# PATH 3 — news sitemap
# ================================================================
_COMMON_SITEMAP_PATHS = [
    "/sitemap-news.xml", "/news-sitemap.xml", "/sitemap_news.xml",
    "/news.xml", "/sitemap.xml", "/sitemap_index.xml",
]

_SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}


def _parse_sitemap_xml(xml_text: str) -> List:
    """Parse a news sitemap and return SimpleNamespace entries."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    entries: List = []
    # Handle both direct URL lists and sitemap-index files.
    # URL list: <urlset><url><loc>...</loc><news:news>...</news></url></urlset>
    for url_el in root.findall("sm:url", _SITEMAP_NS):
        loc = url_el.findtext("sm:loc", default="", namespaces=_SITEMAP_NS)
        lastmod = url_el.findtext("sm:lastmod", default="", namespaces=_SITEMAP_NS)
        news = url_el.find("news:news", _SITEMAP_NS)
        title = ""
        pub = lastmod
        if news is not None:
            title = news.findtext("news:title", default="",
                                  namespaces=_SITEMAP_NS) or ""
            pub = news.findtext("news:publication_date", default=pub,
                                namespaces=_SITEMAP_NS) or pub
        if not loc:
            continue
        entries.append(SimpleNamespace(
            link=loc, title=title, summary="", published=pub,
        ))
    return entries


def collect_from_sitemap(source: Dict, client: httpx.Client) -> List:
    """Try the configured sitemap, then common paths."""
    site = source.get("site")
    if not site:
        return []
    candidates: List[str] = []
    override = source.get("sitemap")
    if override:
        candidates.append(override)
    base = site.rstrip("/")
    candidates.extend(base + p for p in _COMMON_SITEMAP_PATHS)
    for candidate in candidates:
        try:
            r = client.get(candidate, headers=BROWSER_HEADERS,
                           timeout=FETCH_TIMEOUT, follow_redirects=True)
            if r.status_code != 200:
                continue
            body = r.text.strip()
            if not body.startswith("<"):
                continue
            entries = _parse_sitemap_xml(body)
            if entries:
                log.info("  [sitemap] %s -> %d entries", candidate, len(entries))
                return entries
        except httpx.HTTPError:
            continue
    return []


# ---------------------------------------------------------------- date parse
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

    For each source we fan out to three discovery paths (RSS, Google News,
    sitemap), merge the candidate URLs, filter for political relevance,
    fetch the article body, and append new rows to inbox.csv.

    Returns the number of new rows appended.
    """
    sources, _ = load_sources(sources_path)
    existing = read_csv(inbox_path)
    seen_urls = {r.get("source_url", "") for r in existing}

    new_rows: List[Dict] = []
    with httpx.Client() as client:
        for source in sources:
            name = source["name"]
            log.info("source: %s", name)

            # Fan out to all three paths. Each returns an iterable of entries.
            all_entries: Dict[str, object] = {}
            path_counts: Dict[str, int] = {}
            for label, fn in (
                ("rss", collect_from_rss),
                ("gnews", collect_from_google_news),
                ("sitemap", collect_from_sitemap),
            ):
                try:
                    entries = fn(source, client)
                except Exception as e:
                    log.warning("  [%s] raised: %s", label, e)
                    entries = []
                path_counts[label] = len(entries)
                for e in entries:
                    link = getattr(e, "link", "") or ""
                    if not link or link in all_entries:
                        continue
                    all_entries[link] = e

            log.info("  candidates: rss=%d gnews=%d sitemap=%d  merged=%d",
                     path_counts.get("rss", 0),
                     path_counts.get("gnews", 0),
                     path_counts.get("sitemap", 0),
                     len(all_entries))

            if not all_entries:
                log.warning("  no candidates found for %s", name)
                continue

            kept = 0
            for url, entry in all_entries.items():
                if kept >= max_per_source:
                    break
                if url in seen_urls:
                    continue
                if not is_political(entry, source):
                    continue
                body = fetch_article_text(url, client)
                if not body:
                    continue
                pub = parse_publication_date(entry)
                row = InboxRow(
                    raw_statement=body,
                    source_name=name,
                    source_url=url,
                    publication_date=pub,
                    collected_at=utc_stamp(),
                    topic="rss",
                    processed="",
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
