#!/usr/bin/env python3
"""
Malta Political Claims - Collector v4
Full article fetching for richer claim extraction.

Flow:
  1. Fetch RSS feeds -> get article URLs
  2. Quick headline check -> is this political/economic/policy?
  3. If yes -> fetch full article text
  4. Send full text to AI -> extract verifiable claims
  5. Score and route to queues

Routing:
  fact_check score 4-5  -> fact_check_queue.csv
  fact_check score 3    -> review_queue.csv
  everything else       -> archive.csv
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

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-haiku-4-5-20251001"
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

RSS_FEEDS = [
    {"name": "Times of Malta",        "url": "https://www.timesofmalta.com/rss/local",   "note": None},
    {"name": "MaltaToday",            "url": "https://www.maltatoday.com.mt/rss/news",   "note": None},
    {"name": "The Malta Independent", "url": "https://www.independent.com.mt/rss",       "note": None},
    {"name": "Lovin Malta",           "url": "https://lovinmalta.com/feed",               "note": None},
    {"name": "Newsbook",              "url": "https://newsbook.com.mt/en/feed",           "note": None},
    {"name": "ONE News",              "url": "https://onenews.com.mt/feed",               "note": "Labour-affiliated"},
]

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

# ── Step 1: Quick political relevance check ────────────────────────────────

POLITICAL_KEYWORDS = [
    "abela", "borg", "caruana", "camilleri", "dalli", "attard",
    "minister", "government", "parliament", "labour", "nationalist",
    "opposition", "budget", "deficit", "debt", "gdp", "economy",
    "inflation", "employment", "pension", "tax", "housing", "rent",
    "hospital", "mater dei", "health", "education", "environment",
    "planning", "gozo", "election", "constitutional", "statute",
    "million", "billion", "percent", "euro", "growth", "spending",
    "policy", "reform", "law", "regulation", "court", "justice",
]

def is_political(title, desc):
    combined = (title + " " + desc).lower()
    return any(kw in combined for kw in POLITICAL_KEYWORDS)

# ── Step 2: Fetch full article text ────────────────────────────────────────

def fetch_article_text(url, max_chars=5000):
    """Fetch and clean full article text from URL."""
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; MaltaClaimCollector/4.0)",
                "Accept": "text/html,application/xhtml+xml",
            }
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        # Strip scripts, styles, nav, footer
        raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r"<nav[^>]*>.*?</nav>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r"<footer[^>]*>.*?</footer>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r"<header[^>]*>.*?</header>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r"<aside[^>]*>.*?</aside>", " ", raw, flags=re.DOTALL | re.IGNORECASE)

        # Strip remaining HTML tags
        text = re.sub(r"<[^>]+>", " ", raw)

        # Clean whitespace
        text = re.sub(r"\s+", " ", text).strip()

        # Return up to max_chars
        return text[:max_chars] if len(text) > 100 else None

    except Exception:
        return None

# ── Step 3: Extract claims from full article text ──────────────────────────

EXTRACTION_SYSTEM_PROMPT = """You are a senior fact-check editor at a Maltese political newsroom.

You will receive the full text of a news article. Your job is to read it carefully
and extract every verifiable claim — especially ones buried inside the article,
not just in the headline.

THE ONE TEST THAT MATTERS:
"Could a journalist verify or disprove this specific claim using published
statistics, official economic data, budget documents, historical records,
legal texts, or official institutional reports?"

EXTRACT these types of claims:
- Specific numbers and statistics (GDP%, debt ratios, euro amounts, counts)
- Comparisons with a measurable benchmark (higher than, doubled since, lowest in EU)
- Timeline facts (deadline passed, law enacted in year X, published since 2023)
- Legal or constitutional facts (law requires, statute states, Constitution mandates)
- Administrative facts (government spent X, ministry awarded Y, board appointed Z)
- Bold specific predictions that are measurable and falsifiable

DO NOT EXTRACT:
- Future promises and conditional proposals (we will, would, plans to, if elected)
- News events that are not factual claims (planning authority met today)
- Unnamed source reporting (sources say, it is understood)
- Opinions, value judgements, slogans
- Vague statements without a measurable component

IMPORTANT: Read the FULL article carefully. Claims are often in:
- Direct quotes from politicians inside the article body
- Statistics cited in the middle of the article
- Legal references mentioned in passing
- Comparisons made by the journalist quoting official data

For each claim, classify it:
editorial_category options:
- fact_check: passes the data verification test
- news_event: happened but not data-verifiable
- proposal: conditional future promise
- media_report: unnamed source based
- rhetoric: opinion or slogan

Score fact_checkability (only for fact_check items):
5 = precise, specific, clear evidence path exists today
4 = strong claim, evidence path likely exists
3 = borderline, needs human judgment

Respond ONLY with valid JSON, no markdown:

{"political": false}

OR

{"political": true, "claims": [
  {
    "atomic_claim": "single clean verifiable claim",
    "speaker": "Full Name or unknown",
    "party": "PL or PN or AD+PD or Independent or unknown",
    "editorial_category": "fact_check or news_event or proposal or media_report or rhetoric",
    "claim_type": "statistical or legal or historical or administrative or comparative or predictive or opinion",
    "fact_checkability_score": 5,
    "evidence_target": "exact source e.g. NSO GDP statistics 2025, Budget 2026 document, Malta Constitution",
    "numeric_flag": true,
    "legal_flag": false,
    "comparison_flag": false,
    "timeframe_present": true,
    "rejection_reason": null
  }
]}
"""

def extract_claims(client, article):
    """Extract claims from article using full text where available."""

    # Use full article text if we have it, fall back to summary
    body = article.get("full_text") or article.get("description") or ""
    title = article.get("title") or ""

    if not body and not title:
        return []

    # Build prompt with full context
    text = "HEADLINE: {}\n\nARTICLE TEXT:\n{}".format(title, body)

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1:
            return []
        data = json.loads(raw[start:end])
        if not data.get("political"):
            return []
        return data.get("claims") or []
    except Exception as e:
        print("     Parse error: {}".format(e))
        return []

# ── RSS fetching ───────────────────────────────────────────────────────────

def clean_xml(raw):
    return bytes(b for b in raw if b >= 32 or b in (9, 10, 13))

def parse_date(text):
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

def fetch_feed(feed, cutoff):
    articles = []
    try:
        req = urllib.request.Request(
            feed["url"],
            headers={"User-Agent": "Mozilla/5.0 (compatible; MaltaClaimCollector/4.0)"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()

        raw = clean_xml(raw)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raw_str = raw.decode("utf-8", errors="replace")
            raw_str = "".join(c for c in raw_str if ord(c) < 128)
            root = ET.fromstring(raw_str.encode("utf-8"))

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
            desc = re.sub(r"<[^>]+>", " ", txt("description") or txt("summary"))
            desc = re.sub(r"\s+", " ", desc).strip()[:800]

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
                    pub_date = parse_date(val)
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

        print("     {}: {} articles".format(feed["name"], len(articles)))

    except Exception as e:
        print("     {}: ERROR - {}".format(feed["name"], e))

    return articles

# ── Routing and saving ─────────────────────────────────────────────────────

def load_existing():
    seen = set()
    for path in OUTPUT_FILES.values():
        if not os.path.exists(path):
            continue
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    text = row.get("atomic_claim", "").strip().lower()
                    if text:
                        seen.add(text)
        except Exception:
            pass
    return seen

def route(claim):
    category = claim.get("editorial_category", "news_event")
    score = int(claim.get("fact_checkability_score") or 1)

    if category == "fact_check":
        if score >= 4:
            return "fact_check"
        if score == 3:
            return "review"
        return "archive"

    return "archive"

def save_claim(claim, article, fetched_at, seen):
    text = claim.get("atomic_claim", "").strip()
    if not text or text.lower() in seen:
        return None

    queue = route(claim)
    path = OUTPUT_FILES[queue]
    exists = os.path.exists(path)

    speaker = claim.get("speaker", "unknown").strip()
    party = claim.get("party", "unknown")
    role = ""
    if speaker in ALL_PEOPLE:
        party, role = ALL_PEOPLE[speaker]

    source = article["source_name"]
    note = article.get("source_note")
    if note:
        source = "{} [{}]".format(source, note)

    score = int(claim.get("fact_checkability_score") or 1)
    category = claim.get("editorial_category", "news_event")

    if queue == "fact_check":
        status = "approved_for_check"
        verifiability = "checkable"
        needs_review = "FALSE"
    elif queue == "review":
        status = "queued_for_review"
        verifiability = "partially_checkable"
        needs_review = "TRUE"
    else:
        status = "archived"
        verifiability = "not_checkable"
        needs_review = "FALSE"

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

    client = anthropic.Anthropic(api_key=API_KEY)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HRS)

    print("=" * 60)
    print("  Malta Political Claims - Collector v4")
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
        print("  No articles found.")
        sys.exit(0)

    # Step 2 — Filter to political articles and fetch full text
    print("\n[Step 2] Checking relevance and fetching full article text...")
    political_articles = []
    for i, article in enumerate(all_articles):
        if not is_political(article["title"], article["description"]):
            continue

        # Fetch full article text
        full_text = fetch_article_text(article["link"])
        if full_text:
            article["full_text"] = full_text
            print("  [{}/{}] {} chars | {}...".format(
                i+1, len(all_articles),
                len(full_text),
                article["title"][:60]
            ))
        else:
            print("  [{}/{}] summary only | {}...".format(
                i+1, len(all_articles),
                article["title"][:60]
            ))

        political_articles.append(article)
        time.sleep(1)  # polite crawl delay

    print("\n  Political articles: {}".format(len(political_articles)))

    # Step 3 — Extract and classify claims
    print("\n[Step 3] Extracting claims from full articles...")
    seen = load_existing()
    counts = {"fact_check": 0, "review": 0, "archive": 0}

    for i, article in enumerate(political_articles):
        claims = extract_claims(client, article)

        for claim in claims:
            result = save_claim(claim, article, fetched_at, seen)
            if result:
                counts[result] += 1
                if result == "fact_check":
                    print("  [FC] {} | {}".format(
                        article["source_name"],
                        claim.get("atomic_claim", "")[:70]
                    ))

        # Pause every 5 articles to avoid rate limits
        if (i + 1) % 5 == 0 and i + 1 < len(political_articles):
            time.sleep(10)

    total = sum(counts.values())
    print("\n" + "=" * 60)
    print("  Articles fetched     : {}".format(len(all_articles)))
    print("  Political articles   : {}".format(len(political_articles)))
    print("  Fact-Check Queue     : +{}".format(counts["fact_check"]))
    print("  Review Queue         : +{}".format(counts["review"]))
    print("  Archive              : +{}".format(counts["archive"]))
    print("  Total saved          : {}".format(total))
    print("=" * 60)


if __name__ == "__main__":
    main()
