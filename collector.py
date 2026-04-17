#!/usr/bin/env python3
"""
Malta Political Claims - Collector v5

Fixes:
  - Per-feed retry with alternate URLs and varied headers
  - More aggressive XML sanitisation for malformed feeds
  - Attribution-first extraction prompt
  - Hard gate: fact_check queue requires measurable signal flag
  - Unknown speaker claims go to archive not fact_check queue
"""

import csv
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET
import re

try:
    import anthropic
except ImportError:
    print("ERROR: pip install anthropic")
    sys.exit(1)

API_KEY      = os.environ.get("ANTHROPIC_API_KEY")
MODEL        = "claude-haiku-4-5-20251001"
LOOKBACK_HRS = 48

DATA_DIR = os.environ.get(
    "CLAIMS_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)

OUTPUT_FILES = {
    "fact_check": os.path.join(DATA_DIR, "fact_check_queue.csv"),
    "review":     os.path.join(DATA_DIR, "review_queue.csv"),
    "archive":    os.path.join(DATA_DIR, "archive.csv"),
}

CSV_COLUMNS = [
    "claim_text", "atomic_claim", "speaker", "role", "party",
    "source_name", "source_url", "publication_date", "fetched_at",
    "editorial_category", "claim_type", "verifiability_status",
    "fact_checkability_score", "evidence_target",
    "numeric_flag", "legal_flag", "comparison_flag", "timeframe_present",
    "needs_human_review", "rejection_reason", "status", "added_by",
]

# ── RSS feeds with alternate URL fallbacks ─────────────────────────────────
# Each feed has a primary URL and optional alternates tried in order.
# Some feeds block GitHub IPs on the www subdomain but allow the bare domain.

RSS_FEEDS = [
    {
        "name": "Times of Malta",
        "urls": [
            "https://timesofmalta.com/rss/local",
            "https://www.timesofmalta.com/rss/local",
            "https://timesofmalta.com/rss/news",
        ],
        "note": None,
    },
    {
        "name": "MaltaToday",
        "urls": [
            "https://www.maltatoday.com.mt/rss/news",
            "https://maltatoday.com.mt/rss/news",
        ],
        "note": None,
    },
    {
        "name": "The Malta Independent",
        "urls": [
            "https://www.independent.com.mt/rss",
            "https://independent.com.mt/rss",
        ],
        "note": None,
    },
    {
        "name": "Lovin Malta",
        "urls": [
            "https://lovinmalta.com/feed",
            "https://lovinmalta.com/feed/",
        ],
        "note": None,
    },
    {
        "name": "Newsbook",
        "urls": [
            "https://newsbook.com.mt/en/feed",
            "https://newsbook.com.mt/feed",
        ],
        "note": None,
    },
    {
        "name": "ONE News",
        "urls": [
            "https://onenews.com.mt/feed",
            "https://www.onenews.com.mt/feed",
        ],
        "note": "Labour-affiliated",
    },
]

# Rotate User-Agents to reduce blocking
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Feedfetcher-Google; (+http://www.google.com/feedfetcher.html)",
]

import random

def get_headers(url):
    domain = re.sub(r"https?://(www\.)?", "", url).split("/")[0]
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer":         "https://www.google.com/",
        "Cache-Control":   "no-cache",
    }

# ── Politicians ────────────────────────────────────────────────────────────

ALL_PEOPLE = {
    "Robert Abela":            ("PL", "Prime Minister"),
    "Ian Borg":                ("PL", "Deputy Prime Minister"),
    "Clyde Caruana":           ("PL", "Minister for Finance and Employment"),
    "Miriam Dalli":            ("PL", "Minister for Environment"),
    "Byron Camilleri":         ("PL", "Minister for Home Affairs"),
    "Chris Bonett":            ("PL", "Minister for Transport"),
    "Jo Etienne Abela":        ("PL", "Minister for Health"),
    "Jonathan Attard":         ("PL", "Minister for Justice"),
    "Clint Camilleri":         ("PL", "Minister for Gozo"),
    "Clayton Bartolo":         ("PL", "Minister for Tourism"),
    "Silvio Schembri":         ("PL", "Minister for Economy"),
    "Owen Bonnici":            ("PL", "Minister for National Heritage"),
    "Roderick Galdes":         ("PL", "Minister for Housing"),
    "Clifton Grima":           ("PL", "Minister for Education"),
    "Stefan Zrinzo Azzopardi": ("PL", "Minister for Lands"),
    "Alex Borg":               ("PN", "Leader of the Opposition"),
    "Adrian Delia":            ("PN", "Shadow Minister for Finance"),
    "Stephen Spiteri":         ("PN", "Shadow Minister for Health"),
    "Darren Carabott":         ("PN", "Shadow Minister for Home Affairs"),
    "Bernard Grech":           ("PN", "Shadow Minister for Infrastructure"),
    "Eve Borg Bonello":        ("PN", "Shadow Minister for Environment"),
    "Beppe Fenech Adami":      ("PN", "Shadow Minister for Foreign Affairs"),
    "Joe Giglio":              ("PN", "Shadow Minister for Justice"),
    "Chris Said":              ("PN", "Shadow Minister for Gozo"),
    "Jerome Caruana Cilia":    ("PN", "Shadow Minister for Economy"),
    "Rebekah Borg":            ("PN", "Shadow Minister for Environment"),
    "Mario de Marco":          ("PN", "Shadow Minister for Tourism"),
}

SPEAKER_NAMES = set(ALL_PEOPLE.keys())

# ── Political relevance gate ───────────────────────────────────────────────

POLITICAL_KEYWORDS = [
    "abela", "borg", "caruana", "camilleri", "dalli", "attard",
    "minister", "government", "parliament", "labour", "nationalist",
    "opposition", "budget", "deficit", "debt", "gdp", "economy",
    "inflation", "employment", "pension", "tax", "housing", "rent",
    "hospital", "mater dei", "health", "education", "environment",
    "planning", "gozo", "election", "constitutional", "statute",
    "million", "billion", "percent", "euro", "growth", "spending",
    "policy", "reform", "law", "regulation", "court", "justice",
    "schembri", "bonnici", "galdes", "grima", "zrinzo", "bartolo",
    "delia", "fenech", "giglio", "carabott", "spiteri",
]

def is_political(title, desc):
    combined = (title + " " + desc).lower()
    return any(kw in combined for kw in POLITICAL_KEYWORDS)

# ── XML sanitisation ───────────────────────────────────────────────────────

def sanitise_xml(raw_bytes):
    """
    Aggressive multi-stage sanitisation.
    Handles: null bytes, control chars, invalid UTF-8, 
    broken HTML entities, Windows-1252 sequences.
    """
    # Stage 1: strip null bytes and control chars (keep tab, LF, CR)
    cleaned = bytes(b for b in raw_bytes if b >= 32 or b in (9, 10, 13))

    # Stage 2: try to decode as UTF-8
    try:
        text = cleaned.decode("utf-8")
    except UnicodeDecodeError:
        # Try latin-1 which never fails
        text = cleaned.decode("latin-1")

    # Stage 3: remove characters that are invalid in XML 1.0
    text = re.sub(
        r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD]",
        "",
        text
    )

    # Stage 4: fix common broken entities
    text = text.replace("&nbsp;", " ")
    text = text.replace("&mdash;", "-")
    text = text.replace("&ndash;", "-")
    text = text.replace("&rsquo;", "'")
    text = text.replace("&lsquo;", "'")
    text = text.replace("&rdquo;", '"')
    text = text.replace("&ldquo;", '"')
    text = text.replace("&amp;amp;", "&amp;")

    return text.encode("utf-8")

# ── RSS fetching with retry and fallback ───────────────────────────────────

def fetch_url_raw(url, timeout=15):
    """Fetch a URL with retry on transient errors. Returns raw bytes or None."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=get_headers(url))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503):
                # Forbidden or rate-limited — try alternate URL, don't retry same
                return None
            if attempt < 2:
                time.sleep(2 ** attempt)
        except urllib.error.URLError as e:
            # DNS failure or connection refused
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                return None
        except Exception:
            if attempt < 2:
                time.sleep(2)
            else:
                return None
    return None

def parse_feed_bytes(raw):
    """Parse RSS/Atom bytes, returning root element or None."""
    clean = sanitise_xml(raw)
    try:
        return ET.fromstring(clean)
    except ET.ParseError:
        # Last resort: strip all non-ASCII
        ascii_only = "".join(c for c in clean.decode("utf-8", errors="replace") if ord(c) < 128)
        try:
            return ET.fromstring(ascii_only.encode("utf-8"))
        except ET.ParseError:
            return None

def fetch_feed(feed, cutoff):
    """Try each URL in the feed's list until one works."""
    articles = []
    last_error = None

    for url in feed["urls"]:
        raw = fetch_url_raw(url)
        if not raw:
            last_error = "no response"
            continue

        root = parse_feed_bytes(raw)
        if root is None:
            last_error = "XML parse failed"
            continue

        # Successful parse
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

        for item in items:
            def txt(tag):
                el = item.find(tag)
                if el is None:
                    el = item.find("{http://www.w3.org/2005/Atom}" + tag)
                return (el.text or "").strip() if el is not None else ""

            title = txt("title")
            desc  = re.sub(r"<[^>]+>", " ", txt("description") or txt("summary"))
            desc  = re.sub(r"\s+", " ", desc).strip()[:800]

            link_el = item.find("link")
            if link_el is not None:
                link = (link_el.text or link_el.get("href") or "").strip()
            else:
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                link = link_el.get("href", "").strip() if link_el is not None else ""

            pub_date = None
            for tag in ["pubDate", "published", "updated"]:
                val = txt(tag)
                if val:
                    pub_date = _parse_date(val)
                    if pub_date:
                        break

            if pub_date and pub_date < cutoff:
                continue

            articles.append({
                "title":       title,
                "description": desc,
                "link":        link,
                "pub_date":    pub_date.strftime("%Y-%m-%d") if pub_date else "unknown",
                "source_name": feed["name"],
                "source_note": feed.get("note"),
                "full_text":   None,
            })

        print("     {} [{}]: {} articles".format(feed["name"], url.split("/")[2], len(articles)))
        return articles  # success — stop trying alternate URLs

    print("     {}: FAILED - {}".format(feed["name"], last_error))
    return []

def _parse_date(text):
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text.strip())
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip()[:20], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None

# ── Full article fetching ──────────────────────────────────────────────────

def fetch_article_text(url, max_chars=5000):
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers=get_headers(url))
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        for tag in ["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]:
            raw = re.sub(
                r"<{0}[^>]*>.*?</{0}>".format(tag),
                " ", raw, flags=re.DOTALL | re.IGNORECASE
            )

        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars] if len(text) > 200 else None
    except Exception:
        return None

# ── Extraction prompt ──────────────────────────────────────────────────────

EXTRACTION_SYSTEM_PROMPT = """You are a senior fact-check editor at a Maltese political newsroom.

You will receive the text of a news article. Extract claims that meet the following criteria.

RULE 1 — ATTRIBUTION IS MANDATORY.
Every claim you extract must be attributable to a specific named person,
named institution, or named official document.

DO NOT extract claims that are:
- Written by the journalist as background context
- General facts without a clear source within the article
- Attributed only to "sources", "insiders", or unnamed parties

If you cannot identify who made the claim, do not extract it.

NAMED SPEAKERS to look for include:
- Government ministers: Robert Abela, Clyde Caruana, Ian Borg, Miriam Dalli,
  Jo Etienne Abela, Jonathan Attard, Clint Camilleri, Byron Camilleri,
  Silvio Schembri, Owen Bonnici, Roderick Galdes, Clifton Grima
- Opposition: Alex Borg, Adrian Delia, Stephen Spiteri, Darren Carabott,
  Bernard Grech, Eve Borg Bonello, Beppe Fenech Adami, Joe Giglio
- Named NGOs, unions, or named individuals with a clear political role

CRITICAL RULE — DATA INSTITUTIONS ARE NOT SPEAKERS:
The following are DATA SOURCES used to VERIFY claims. They are NOT claim-makers.
NEVER set the speaker to any of these — if no political actor made the claim, do not extract it:
  NSO, Eurostat, European Commission, IMF, World Bank, Central Bank of Malta,
  Planning Authority, Housing Authority, Electoral Commission, TomTom,
  Transparency International, FocusEconomics, Allianz Trade, KPMG,
  any statistics office, any ratings agency, any research institution

A claim extracted with speaker = Eurostat or speaker = NSO is WRONG.
Eurostat and NSO are where we go to CHECK claims — they are not the source of claims.

The correct pattern is:
  GOOD: Robert Abela said "Malta has the highest employment rate in the EU"
        → speaker = Robert Abela, verify against Eurostat
  BAD:  Eurostat data shows Malta's employment rate is 83.6%
        → This is a data point, not a political claim. Do not extract it.

RULE 2 — THE VERIFIABILITY TEST.
A claim only enters the fact_check category if a journalist could verify
or disprove it today using:
- Published statistics or official data (NSO, Eurostat, IMF, Central Bank)
- Budget or government financial documents
- Legal texts, the Constitution, statutes, legal notices
- Official institutional records or parliamentary records
- Historical documented facts

STRONG SIGNALS — prioritise claims containing:
- Euro amounts (€X million, €X billion)
- Percentages or ratios (X% of GDP, X% increase)
- Named comparisons (highest in EU, double since 2013)
- Specific years or dates (since 2023, in Q3 2025, by March 2026)
- Legal references (Constitution Article X, legal notice Y, statute requires)
- Administrative facts (government spent, ministry awarded, board delayed)

WEAK SIGNALS — do not extract as fact_check:
- General quality of life statements
- Vague administrative descriptions ("a new system was introduced")
- Future-tense promises or proposals (we will, would, plans to, if elected)
- Rhetorical comparisons without data ("better than before")

RULE 3 — CLASSIFICATION.
For each extracted claim, assign:

editorial_category:
  fact_check     — attributed, verifiable with data
  news_event     — happened, attributed, but not data-verifiable
  proposal       — attributed future promise or conditional intention
  media_report   — based on unnamed sources
  rhetoric       — opinion, slogan, value judgement

claim_type:
  statistical | legal | historical | administrative | comparative | predictive | opinion

fact_checkability_score (only for fact_check):
  5 = precise, specific, exact evidence path exists today
  4 = strong, evidence path likely exists
  3 = borderline, needs human judgment

RULE 4 — DO NOT EXTRACT:
- Journalist background narration not attributed to anyone
- Generic news events (meeting held, vote took place)
- Unnamed source reports
- Proposals, promises, future intentions
- Opinions, attacks, slogans

Respond ONLY with valid JSON, no markdown, no backticks.

If not political: {"political": false}

If political with claims:
{"political": true, "claims": [
  {
    "atomic_claim": "the exact verifiable claim in clean language",
    "speaker": "Full Name — search the article carefully. Use the institution name if no person is named (e.g. Planning Authority, Housing Authority, NSO). If genuinely unattributable write General Public Statement — never write unknown",
    "party": "PL or PN or AD+PD or Independent or institution or unknown",
    "editorial_category": "fact_check or news_event or proposal or media_report or rhetoric",
    "claim_type": "statistical or legal or historical or administrative or comparative or predictive or opinion",
    "fact_checkability_score": 5,
    "evidence_target": "specific source e.g. NSO GDP statistics 2025, Budget 2026 document, Malta Constitution Article 97",
    "numeric_flag": true,
    "legal_flag": false,
    "comparison_flag": false,
    "timeframe_present": true,
    "rejection_reason": null
  }
]}
"""

def extract_claims(client, article):
    body  = article.get("full_text") or article.get("description") or ""
    title = article.get("title") or ""
    if not body and not title:
        return []

    text = "HEADLINE: {}\n\nARTICLE TEXT:\n{}".format(title, body)

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw   = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start == -1:
            return []
        data = json.loads(raw[start:end])
        if not data.get("political"):
            return []
        return data.get("claims") or []
    except Exception as e:
        print("     Parse error: {}".format(e))
        return []

# ── Routing ────────────────────────────────────────────────────────────────

# Data institutions are sources to verify AGAINST — never claim-makers
DATA_INSTITUTIONS = {
    "nso", "eurostat", "european commission", "imf", "world bank",
    "central bank of malta", "central bank", "planning authority",
    "housing authority", "electoral commission", "tomtom", "inrix",
    "transparency international", "focuseconomics", "allianz trade",
    "kpmg", "pwc", "oecd", "un", "united nations", "who",
    "european environment agency", "eea", "era", "transport malta",
    "infrastructure malta", "mcast", "university of malta",
    "national statistics office", "statistics office",
    "scope ratings", "fitch", "moody", "standard & poor",
    "global property guide", "investropa", "amphora media",
    "wikipedia", "grokipedia", "reference", "research institution",
    "news outlet", "international institution", "financial institution",
    "academic institution", "statistics office",
}

def route(claim):
    """
    Route by editorial category first, score second.
    Extra gate: fact_check queue requires at least one measurable signal flag.
    Claims with data institution speakers go to archive — they are data points not claims.
    Claims with unknown speaker go to review for human attribution.
    """
    category = claim.get("editorial_category", "news_event")
    score    = int(claim.get("fact_checkability_score") or 1)
    speaker  = (claim.get("speaker") or "").strip().lower()

    if category == "fact_check":
        # Gate 1: data institutions are sources not speakers — archive these
        if any(inst in speaker for inst in DATA_INSTITUTIONS):
            return "archive"

        # Gate 2: unknown speaker goes to review for human attribution
        if speaker in ("unknown", "", "journalist", "reporter", "general public statement"):
            return "review"

        # Gate 2: must have at least one measurable signal
        has_signal = (
            claim.get("numeric_flag")     is True or
            claim.get("legal_flag")       is True or
            claim.get("comparison_flag")  is True or
            claim.get("timeframe_present") is True
        )
        if not has_signal and score < 5:
            return "review"

        # Route by score
        if score >= 4:
            return "fact_check"
        if score == 3:
            return "review"
        return "archive"

    # Everything else goes to archive
    return "archive"

# ── Saving ─────────────────────────────────────────────────────────────────

def load_existing():
    seen = set()
    for path in OUTPUT_FILES.values():
        if not os.path.exists(path):
            continue
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    t = row.get("atomic_claim", "").strip().lower()
                    if t:
                        seen.add(t)
        except Exception:
            pass
    return seen

def save_claim(claim, article, fetched_at, seen):
    text = claim.get("atomic_claim", "").strip()
    if not text or text.lower() in seen:
        return None

    queue = route(claim)
    path  = OUTPUT_FILES[queue]
    exists = os.path.exists(path)

    speaker = claim.get("speaker", "unknown").strip()
    party   = claim.get("party", "unknown")
    role    = ""
    if speaker in ALL_PEOPLE:
        party, role = ALL_PEOPLE[speaker]

    source = article["source_name"]
    note   = article.get("source_note")
    if note:
        source = "{} [{}]".format(source, note)

    score    = int(claim.get("fact_checkability_score") or 1)
    category = claim.get("editorial_category", "news_event")

    if queue == "fact_check":
        status, verifiability, needs_review = "approved_for_check", "checkable", "FALSE"
    elif queue == "review":
        status, verifiability, needs_review = "queued_for_review", "partially_checkable", "TRUE"
    else:
        status, verifiability, needs_review = "archived", "not_checkable", "FALSE"

    row = {
        "claim_text":              article["title"],
        "atomic_claim":            text,
        "speaker":                 speaker,
        "role":                    role,
        "party":                   party,
        "source_name":             source,
        "source_url":              article.get("link", ""),
        "publication_date":        article.get("pub_date", "unknown"),
        "fetched_at":              fetched_at,
        "editorial_category":      category,
        "claim_type":              claim.get("claim_type", ""),
        "verifiability_status":    verifiability,
        "fact_checkability_score": score,
        "evidence_target":         claim.get("evidence_target") or "",
        "numeric_flag":            claim.get("numeric_flag", False),
        "legal_flag":              claim.get("legal_flag", False),
        "comparison_flag":         claim.get("comparison_flag", False),
        "timeframe_present":       claim.get("timeframe_present", False),
        "needs_human_review":      needs_review,
        "rejection_reason":        claim.get("rejection_reason") or "",
        "status":                  status,
        "added_by":                "collector",
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)

    seen.add(text.lower())
    return queue

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    client     = anthropic.Anthropic(api_key=API_KEY)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cutoff     = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HRS)

    print("=" * 60)
    print("  Malta Political Claims - Collector v5")
    print("  Run time     : {}".format(fetched_at))
    print("  Looking back : {} hours".format(LOOKBACK_HRS))
    print("  Data folder  : {}".format(DATA_DIR))
    print("=" * 60)

    # Step 1 — Fetch RSS feeds
    print("\n[Step 1] Fetching RSS feeds...")
    all_articles = []
    for feed in RSS_FEEDS:
        articles = fetch_feed(feed, cutoff)
        all_articles.extend(articles)

    print("\n  Total articles: {}".format(len(all_articles)))
    if not all_articles:
        print("  No articles found. Check feed errors above.")
        sys.exit(0)

    # Step 2 — Filter to political and fetch full text
    print("\n[Step 2] Filtering and fetching full article text...")
    political_articles = []
    for i, article in enumerate(all_articles):
        if not is_political(article["title"], article["description"]):
            continue
        full_text = fetch_article_text(article["link"])
        if full_text:
            article["full_text"] = full_text
            label = "{} chars".format(len(full_text))
        else:
            label = "summary only"
        print("  [{}/{}] {} | {}...".format(
            i+1, len(all_articles), label, article["title"][:55]
        ))
        political_articles.append(article)
        time.sleep(1)

    print("\n  Political articles: {}".format(len(political_articles)))

    # Step 3 — Extract and classify claims
    print("\n[Step 3] Extracting claims...")
    seen   = load_existing()
    counts = {"fact_check": 0, "review": 0, "archive": 0}

    for i, article in enumerate(political_articles):
        claims = extract_claims(client, article)
        for claim in claims:
            result = save_claim(claim, article, fetched_at, seen)
            if result:
                counts[result] += 1
                if result == "fact_check":
                    print("  [FC] {} | {} | {}".format(
                        claim.get("speaker", "?")[:20],
                        article["source_name"],
                        claim.get("atomic_claim", "")[:55]
                    ))
        if (i + 1) % 5 == 0 and i + 1 < len(political_articles):
            time.sleep(10)

    total = sum(counts.values())
    print("\n" + "=" * 60)
    print("  Articles fetched   : {}".format(len(all_articles)))
    print("  Political articles : {}".format(len(political_articles)))
    print("  Fact-Check Queue   : +{}".format(counts["fact_check"]))
    print("  Review Queue       : +{}".format(counts["review"]))
    print("  Archive            : +{}".format(counts["archive"]))
    print("  Total saved        : {}".format(total))
    print("=" * 60)


if __name__ == "__main__":
    main()
