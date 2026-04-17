#!/usr/bin/env python3
"""
Malta Political Claims - Collector v6.1

Purpose:
  Capture claims, not verdicts.

No separate inbox file:
  - strong captured claims go directly to fact_check_queue.csv
  - borderline / proposal / media-report / rhetorical claims go to review_queue.csv
  - obvious non-claims or unusable extractions go to archive.csv

A separate analyser can later re-triage items already placed in review_queue.csv.
"""

import csv
import json
import os
import re
import sys
import time
import random
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

try:
    import anthropic
except ImportError:
    print("ERROR: pip install anthropic")
    sys.exit(1)

API_KEY      = os.environ.get("ANTHROPIC_API_KEY")
MODEL        = os.environ.get("CLAIMS_MODEL", "claude-haiku-4-5-20251001")
LOOKBACK_HRS = int(os.environ.get("CLAIMS_LOOKBACK_HRS", "48"))

DATA_DIR = os.environ.get(
    "CLAIMS_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)

FACT_FILE    = os.path.join(DATA_DIR, "fact_check_queue.csv")
REVIEW_FILE  = os.path.join(DATA_DIR, "review_queue.csv")
ARCHIVE_FILE = os.path.join(DATA_DIR, "archive.csv")

CSV_COLUMNS = [
    "claim_text", "atomic_claim", "speaker", "role", "party",
    "speaker_type", "attribution_type", "attribution_confidence",
    "source_name", "source_url", "publication_date", "fetched_at",
    "article_title", "article_summary", "claim_family", "claim_type",
    "editorial_category", "claim_strength", "evidence_target",
    "numeric_flag", "legal_flag", "comparison_flag", "timeframe_present",
    "proposal_flag", "media_report_flag", "rhetoric_flag",
    "duplicate_group", "status", "added_by",
]

RSS_FEEDS = [
    {"name": "Times of Malta", "urls": ["https://timesofmalta.com/rss/local", "https://www.timesofmalta.com/rss/local", "https://timesofmalta.com/rss/news"], "note": None},
    {"name": "MaltaToday", "urls": ["https://www.maltatoday.com.mt/rss/news", "https://maltatoday.com.mt/rss/news"], "note": None},
    {"name": "The Malta Independent", "urls": ["https://www.independent.com.mt/rss", "https://independent.com.mt/rss"], "note": None},
    {"name": "Lovin Malta", "urls": ["https://lovinmalta.com/feed", "https://lovinmalta.com/feed/"], "note": None},
    {"name": "Newsbook", "urls": ["https://newsbook.com.mt/en/feed", "https://newsbook.com.mt/feed"], "note": None},
    {"name": "ONE News", "urls": ["https://onenews.com.mt/feed", "https://www.onenews.com.mt/feed"], "note": "Labour-affiliated"},
]

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Feedfetcher-Google; (+http://www.google.com/feedfetcher.html)",
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
    "Bernard Grech":           ("PN", "Former PN Leader / MP"),
    "Eve Borg Bonello":        ("PN", "Shadow Minister for Environment"),
    "Beppe Fenech Adami":      ("PN", "Shadow Minister for Foreign Affairs"),
    "Joe Giglio":              ("PN", "Shadow Minister for Justice"),
    "Chris Said":              ("PN", "Shadow Minister for Gozo"),
    "Jerome Caruana Cilia":    ("PN", "Shadow Minister for Economy"),
    "Rebekah Borg":            ("PN", "Shadow Minister / MP"),
    "Mario de Marco":          ("PN", "Shadow Minister for Tourism"),
    "Sandra Gauci":            ("AD+PD", "ADPD Chairperson"),
}

POLITICAL_KEYWORDS = [
    "abela", "borg", "caruana", "camilleri", "dalli", "attard",
    "minister", "government", "parliament", "labour", "nationalist",
    "opposition", "budget", "deficit", "debt", "gdp", "economy",
    "inflation", "employment", "pension", "tax", "housing", "rent",
    "hospital", "mater dei", "health", "education", "environment",
    "planning", "gozo", "election", "constitutional", "statute",
    "million", "billion", "percent", "euro", "growth", "spending",
    "policy", "reform", "law", "regulation", "court", "justice",
    "party", "manifesto", "survey", "poll", "scheme", "ministerial",
]

EXTRACTION_SYSTEM_PROMPT = """You are a claim-capture editor for a Maltese political claims database.

Your job is NOT to decide whether claims are true. Your job is ONLY to capture real claims made in the article.

CRITICAL PRINCIPLE:
Keep real political claims. Reject non-claim background text.

A good extracted item must be:
1. A testable assertion about reality — something that can be confirmed or refuted against
   a specific dataset, law, published document, or observable fact.
2. Attributable to a named political actor — a politician, minister, party leader, or party.
3. Written as a single atomic claim.

THE DECOMPOSITION RULE — this is the most important instruction:
When a politician justifies an action with a factual assertion, extract the assertion — not the action.
The action is an announcement. The assertion is the claim.

Examples of correct decomposition:
  Politician says: "We are proposing this equity-sharing model because young people cannot afford
                   to buy property and housing prices have doubled in the last decade."
  BAD extraction: "The PN proposed an equity-sharing model."         ← this is the announcement
  GOOD extraction: "Alex Borg said housing prices have doubled in the last decade."   ← this is the claim
  GOOD extraction: "Alex Borg said young people cannot afford to buy property in Malta." ← this is the claim

  Politician says: "We are cutting taxes because Malta now has the highest employment rate in the EU."
  BAD extraction: "The government is cutting taxes."
  GOOD extraction: "Clyde Caruana said Malta has the highest employment rate in the EU."

WHAT TO EXTRACT:
- statistical claims (numbers, percentages, amounts, rankings)
- legal or constitutional claims (law requires, statute mandates, constitution states)
- historical claims (since year X, compared to year Y, before 2013)
- comparative claims (highest in EU, worse than average, doubled since)
- administrative claims (government spent, ministry delayed, deadline passed)
- falsifiable conditions (nobody can afford housing, waiting lists have grown, delays are 7 months)
- justifications and reasoning stated by politicians as facts

WHAT NOT TO EXTRACT:
- the proposal or announcement itself ("we will introduce", "we propose", "we plan")
- journalist background narration with no named political actor asserting it
- pure meta-political statements ("this will help families", "this is the right approach")
- article metadata, navigation text, boilerplate
- duplicate phrasing of the same claim within the same article
- unnamed source reports ("sources say", "it is understood")

VAGUE CLAIMS — do not discard, do label:
If a claim is politically interesting but lacks a specific number or date, still extract it
but set claim_strength to "weak". A human editor can decide whether to keep it.
Example: "Alex Borg said Malta's infrastructure cannot handle current population levels."
This is vague but testable — keep it at weak strength.

DO NOT extract if the speaker is a data institution such as NSO, Eurostat, IMF,
European Commission, Central Bank, or any statistics office or research body.
These are sources used to VERIFY claims — they do not make political claims.

For each extracted claim, output:
- atomic_claim
- speaker
- party
- speaker_type: politician | institution | party | media_report | unknown
- attribution_type: direct_quote | indirect_quote | article_report | unnamed_source
- attribution_confidence: high | medium | low
- claim_family: factual_assertion | proposal | justification | attack | interpretation
- claim_type: statistical | legal | historical | administrative | comparative | predictive | rhetorical | opinion | policy
- editorial_category: claim | proposal | media_report | rhetoric
- claim_strength: strong | medium | weak
- evidence_target
- numeric_flag
- legal_flag
- comparison_flag
- timeframe_present
- proposal_flag
- media_report_flag
- rhetoric_flag

If the article is not political or policy-relevant, return:
{"political": false}

Otherwise return ONLY valid JSON:
{"political": true, "claims": [ ... ]}
"""


def get_headers(url):
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/rss+xml, application/xml, text/xml, text/html, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": "https://www.google.com/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


def is_political(title, desc):
    combined = (title + " " + desc).lower()
    return any(kw in combined for kw in POLITICAL_KEYWORDS)


def sanitise_xml(raw_bytes):
    cleaned = bytes(b for b in raw_bytes if b >= 32 or b in (9, 10, 13))
    try:
        text = cleaned.decode("utf-8")
    except UnicodeDecodeError:
        text = cleaned.decode("latin-1", errors="replace")
    text = re.sub(r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD]", "", text)
    replacements = {"&nbsp;": " ", "&mdash;": "-", "&ndash;": "-", "&rsquo;": "'", "&lsquo;": "'", "&rdquo;": '"', "&ldquo;": '"', "&amp;amp;": "&amp;"}
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text.encode("utf-8")


def fetch_url_raw(url, timeout=15):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=get_headers(url))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503):
                return None
            if attempt < 2:
                time.sleep(2 ** attempt)
        except Exception:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                return None
    return None


def parse_feed_bytes(raw):
    clean = sanitise_xml(raw)
    try:
        return ET.fromstring(clean)
    except ET.ParseError:
        ascii_only = "".join(c for c in clean.decode("utf-8", errors="replace") if ord(c) < 128)
        try:
            return ET.fromstring(ascii_only.encode("utf-8"))
        except ET.ParseError:
            return None


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


def fetch_feed(feed, cutoff):
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
        items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for item in items:
            def txt(tag):
                el = item.find(tag) or item.find("{http://www.w3.org/2005/Atom}" + tag)
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
                    pub_date = _parse_date(val)
                    if pub_date:
                        break
            if pub_date and pub_date < cutoff:
                continue
            articles.append({
                "title": title,
                "description": desc,
                "link": link,
                "pub_date": pub_date.strftime("%Y-%m-%d") if pub_date else "unknown",
                "source_name": feed["name"],
                "source_note": feed.get("note"),
                "full_text": None,
            })
        print(f"     {feed['name']} [{url.split('/')[2]}]: {len(articles)} articles")
        return articles
    print(f"     {feed['name']}: FAILED - {last_error}")
    return []


def fetch_article_text(url, max_chars=5000):
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers=get_headers(url))
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        for tag in ["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]:
            raw = re.sub(r"<{0}[^>]*>.*?</{0}>".format(tag), " ", raw, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars] if len(text) > 200 else None
    except Exception:
        return None


def extract_claims(client, article):
    body = article.get("full_text") or article.get("description") or ""
    title = article.get("title") or ""
    if not body and not title:
        return []
    text = f"HEADLINE: {title}\n\nARTICLE TEXT:\n{body}"
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2200,
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
        print(f"     Parse error: {e}")
        return []


def load_existing():
    seen = set()
    for path in [FACT_FILE, REVIEW_FILE, ARCHIVE_FILE]:
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


def append_row(path, row):
    exists = os.path.exists(path)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def collector_route(claim):
    speaker = (claim.get("speaker") or "").strip().lower()
    attribution_conf = (claim.get("attribution_confidence") or "medium").lower()
    category = (claim.get("editorial_category") or "claim").lower()
    atomic = (claim.get("atomic_claim") or "").strip()

    if not atomic or len(atomic) < 12:
        return "archive", "not_really_a_claim"

    if speaker in ("", "unknown") and attribution_conf == "low":
        return "archive", "weak_unattributed_extraction"

    if category == "proposal":
        return "review", "proposal_captured_for_later_analysis"
    if category == "media_report":
        return "review", "media_report_captured_for_later_analysis"
    if category == "rhetoric":
        return "review", "rhetorical_claim_captured_for_later_analysis"

    strength = (claim.get("claim_strength") or "medium").lower()

    # Weak claims — vague but potentially interesting — go to review for human decision
    if strength == "weak":
        return "review", "weak_claim_needs_human_review"

    has_signal = any([
        claim.get("numeric_flag") is True,
        claim.get("legal_flag") is True,
        claim.get("comparison_flag") is True,
        claim.get("timeframe_present") is True,
    ])
    if attribution_conf == "high" and has_signal:
        return "fact_check", "strong_attributed_claim_candidate"
    return "review", "captured_claim_needs_downstream_analysis"


def save_claim(claim, article, fetched_at, seen):
    text = (claim.get("atomic_claim") or "").strip()
    if not text or text.lower() in seen:
        return None

    speaker = (claim.get("speaker") or "unknown").strip()
    party = claim.get("party", "unknown")
    role = ""
    if speaker in ALL_PEOPLE:
        party, role = ALL_PEOPLE[speaker]

    source = article["source_name"]
    if article.get("source_note"):
        source = f"{source} [{article['source_note']}]"

    queue, reason = collector_route(claim)
    dest = FACT_FILE if queue == "fact_check" else REVIEW_FILE if queue == "review" else ARCHIVE_FILE
    row = {
        "claim_text": article["title"],
        "atomic_claim": text,
        "speaker": speaker,
        "role": role,
        "party": party,
        "speaker_type": claim.get("speaker_type", "unknown"),
        "attribution_type": claim.get("attribution_type", "unknown"),
        "attribution_confidence": claim.get("attribution_confidence", "medium"),
        "source_name": source,
        "source_url": article.get("link", ""),
        "publication_date": article.get("pub_date", "unknown"),
        "fetched_at": fetched_at,
        "article_title": article.get("title", ""),
        "article_summary": article.get("description", ""),
        "claim_family": claim.get("claim_family", "factual_assertion"),
        "claim_type": claim.get("claim_type", ""),
        "editorial_category": claim.get("editorial_category", "claim"),
        "claim_strength": claim.get("claim_strength", "medium"),
        "evidence_target": claim.get("evidence_target", ""),
        "numeric_flag": claim.get("numeric_flag", False),
        "legal_flag": claim.get("legal_flag", False),
        "comparison_flag": claim.get("comparison_flag", False),
        "timeframe_present": claim.get("timeframe_present", False),
        "proposal_flag": claim.get("proposal_flag", False),
        "media_report_flag": claim.get("media_report_flag", False),
        "rhetoric_flag": claim.get("rhetoric_flag", False),
        "duplicate_group": "",
        "status": "captured_pending_analysis" if queue == "review" else "captured_strong_candidate" if queue == "fact_check" else reason,
        "added_by": "collector",
    }
    append_row(dest, row)
    seen.add(text.lower())
    return queue


def main():
    if not API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=API_KEY)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HRS)

    print("=" * 60)
    print("  Malta Political Claims - Collector v6.1")
    print(f"  Run time     : {fetched_at}")
    print(f"  Looking back : {LOOKBACK_HRS} hours")
    print(f"  Data folder  : {DATA_DIR}")
    print("=" * 60)

    print("\n[Step 1] Fetching RSS feeds...")
    all_articles = []
    for feed in RSS_FEEDS:
        all_articles.extend(fetch_feed(feed, cutoff))
    print(f"\n  Total articles: {len(all_articles)}")
    if not all_articles:
        print("  No articles found. Check feed errors above.")
        sys.exit(0)

    print("\n[Step 2] Filtering and fetching full article text...")
    political_articles = []
    for i, article in enumerate(all_articles):
        if not is_political(article["title"], article["description"]):
            continue
        full_text = fetch_article_text(article["link"])
        article["full_text"] = full_text
        label = f"{len(full_text)} chars" if full_text else "summary only"
        print(f"  [{i+1}/{len(all_articles)}] {label} | {article['title'][:55]}...")
        political_articles.append(article)
        time.sleep(1)
    print(f"\n  Political articles: {len(political_articles)}")

    print("\n[Step 3] Extracting claims...")
    seen = load_existing()
    counts = {"fact_check": 0, "review": 0, "archive": 0}
    for i, article in enumerate(political_articles):
        claims = extract_claims(client, article)
        for claim in claims:
            result = save_claim(claim, article, fetched_at, seen)
            if result:
                counts[result] += 1
                if result == "fact_check":
                    print(f"  [FC] {claim.get('speaker','?')[:20]} | {article['source_name']} | {claim.get('atomic_claim','')[:55]}")
        if (i + 1) % 5 == 0 and i + 1 < len(political_articles):
            time.sleep(10)

    total = sum(counts.values())
    print("\n" + "=" * 60)
    print(f"  Articles fetched   : {len(all_articles)}")
    print(f"  Political articles : {len(political_articles)}")
    print(f"  Fact-Check Queue   : +{counts['fact_check']}")
    print(f"  Review Queue       : +{counts['review']}")
    print(f"  Archive            : +{counts['archive']}")
    print(f"  Total saved        : {total}")
    print("=" * 60)


if __name__ == "__main__":
    main()
