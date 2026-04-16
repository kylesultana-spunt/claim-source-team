#!/usr/bin/env python3
"""
Malta Political Claims - Collector v2
Editorial-grade extraction with LLM classification.

Five editorial categories:
  fact_check           - verifiable claim about present/past/future reality
  proposal_announcement - conditional promise, policy intention, vague plan
  reasoning_review     - not false but flawed framing, selective omission, misleading causation
  media_source_report  - unnamed source reporting, cannot verify at capture
  rhetoric             - opinion, slogan, attack, value judgement

Routing:
  fact_check (score 4-5)        -> fact_check_queue.csv
  fact_check (score 3)          -> review_queue.csv
  reasoning_review              -> review_queue.csv
  media_source_report           -> review_queue.csv (if time-sensitive, else excluded)
  proposal_announcement         -> excluded (not a fact-check item)
  rhetoric                      -> rhetoric_archive.csv
"""

import csv
import json
import os
import sys
import time
import urllib.request
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
    "rhetoric":   os.path.join(DATA_DIR, "rhetoric_archive.csv"),
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

SYSTEM_PROMPT = """You are a senior fact-check editor at a Maltese political newsroom.

You receive a news article headline and summary. Your job is editorial triage.

STEP 1 - Is this article political or policy-related?
If no, respond: {"political": false}

STEP 2 - Extract individual statements from the article.
For each statement, apply the following editorial test:

THE CORE TEST: Does this statement contain a TESTABLE PROPOSITION?
A testable proposition is something that can be confirmed or refuted against
publicly available evidence - numbers, records, laws, official data, or observable reality.

This applies regardless of tense. A future-tense claim CAN be testable if it is
specific, measurable, and falsifiable.

TESTABLE (extract these):
- "Malta's GDP doubled in 10 years" - specific, measurable, verifiable
- "Debt fell from 70% to 47% of GDP" - specific, measurable, verifiable
- "Malta grew faster than any EU country last year" - comparative, measurable
- "The law requires a two-thirds majority" - legal fact, verifiable
- "Ministerial declarations have not been published since 2023" - administrative, verifiable
- "Malta will reach UK GDP levels within 2 years" - bold specific prediction, falsifiable
- "Government revenue rose by 1.2 billion euros" - statistical, verifiable
- "The constitutional deadline passed in March 2026" - timeline, verifiable

NOT TESTABLE (do not extract these as fact_check):
- "A PN government would reduce VAT to 7 percent for restaurants" - conditional on future event not yet happened
- "We plan to build a new hospital" - vague intention, no benchmark
- "We will improve people's lives" - no measurable benchmark
- "Three new schemes were announced" - announcement, not a factual claim
- "A new ferry route is planned" - future intention, not yet real
- "Sources say Abela ruled out May election" - unnamed source, cannot verify

STEP 3 - For each extracted statement, classify it into one of five categories:

1. fact_check
   A verifiable statement about present or past reality, trends, numbers,
   timelines, law, administration, or historical fact.
   Also includes bold specific predictions that are measurable and falsifiable.
   STRONG SIGNALS: euro amounts, percentages, ratios, years, rankings,
   legal references, published/unpublished, approved/rejected, deadline,
   highest/lowest, rose/fell/doubled/tripled, from X to Y, since [year]

2. proposal_announcement
   A conditional promise, policy intention, planned reform, or future measure
   that depends on a future event (winning an election, passing a law, etc.).
   These are NOT fact-checkable yet because they describe what someone would do,
   not what is or was.
   SIGNALS: would, plans to, proposes, intends to, within first 100 days,
   if elected, a PN/PL government will, we are committed to

3. reasoning_review
   A statement that is not directly true or false but contains:
   - selective framing or cherry-picked comparison
   - omission of relevant context
   - false novelty (presenting existing policy as new)
   - exaggerated causal claims
   - misleading implication without direct falsehood
   These are worth examining but are not straightforward fact-check items.

4. media_source_report
   A claim based mainly on unnamed sources or insider reporting where the
   underlying assertion cannot be independently verified at capture stage.
   SIGNALS: "sources say", "according to insiders", "it is understood that",
   "party sources told", "reports suggest"

5. rhetoric
   Pure opinion, slogan, emotional appeal, personal attack, value judgement,
   or generic campaign language with no testable content.
   SIGNALS: "best government ever", "failed the people", "we believe",
   "our vision", "the people deserve", "negative rhetoric"

STEP 4 - Score each claim on fact_checkability (1-5):
5 = precise, specific, clear evidence path exists (e.g. NSO data, budget document, Constitution)
4 = strong claim, evidence path exists but may need context
3 = partially checkable, borderline, needs human judgment
2 = weak, vague, or missing key specifics
1 = not checkable (should be rhetoric or proposal)

ROUTING RULES (do not include in output, applied internally):
- fact_check score 4-5 -> Fact-Check Queue
- fact_check score 3 -> Review Queue
- reasoning_review -> Review Queue
- media_source_report -> Review Queue only if time-sensitive, else exclude
- proposal_announcement -> EXCLUDE (do not return these)
- rhetoric -> Rhetoric Archive

IMPORTANT: Be strict. It is better to capture fewer, stronger claims than many weak ones.
The Fact-Check Queue should contain only items a journalist could actually verify today.

Respond ONLY with valid JSON. No markdown, no backticks.

If not political: {"political": false}

If political:
{"political": true, "claims": [
  {
    "atomic_claim": "the single cleaned testable claim",
    "speaker": "Full Name or unknown",
    "party": "PL or PN or AD+PD or Independent or unknown",
    "editorial_category": "fact_check or reasoning_review or media_source_report or rhetoric",
    "claim_type": "statistical or legal or historical or administrative or comparative or predictive or reasoning or opinion",
    "fact_checkability_score": 5,
    "verifiability_status": "checkable or partially_checkable or not_checkable",
    "evidence_target": "specific source to verify against e.g. NSO statistics, Budget 2026 document, Malta Constitution",
    "numeric_flag": true,
    "legal_flag": false,
    "comparison_flag": false,
    "timeframe_present": true,
    "needs_human_review": false,
    "rejection_reason": null,
    "status": "approved_for_check or queued_for_review or archived_rhetoric"
  }
]}

Note: Do NOT include proposal_announcement items in the output at all.
"""


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
            headers={"User-Agent": "Mozilla/5.0 (compatible; MaltaClaimCollector/2.0)"}
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
            desc = re.sub(r"\s+", " ", desc).strip()[:600]

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
                "title": title,
                "description": desc,
                "link": link,
                "pub_date": pub_date.strftime("%Y-%m-%d") if pub_date else "unknown",
                "source_name": feed["name"],
                "source_note": feed.get("note"),
            })

        print("     {}: {} articles".format(feed["name"], len(articles)))

    except Exception as e:
        print("     {}: ERROR - {}".format(feed["name"], e))

    return articles


def process_article(client, article):
    text = "Headline: {}\n\nSummary: {}".format(article["title"], article["description"])
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            system=SYSTEM_PROMPT,
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
    category = claim.get("editorial_category", "rhetoric")
    score = int(claim.get("fact_checkability_score", 1))

    if category == "fact_check":
        if score >= 4:
            return "fact_check"
        if score == 3:
            return "review"
        return None  # score 1-2 fact_check claim is too weak, exclude

    if category in ("reasoning_review", "media_source_report"):
        return "review"

    if category == "rhetoric":
        return "rhetoric"

    # proposal_announcement -> excluded
    return None


def save_claim(claim, article, fetched_at, seen):
    text = claim.get("atomic_claim", "").strip()
    if not text or text.lower() in seen:
        return None

    queue = route(claim)
    if queue is None:
        return None  # excluded

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

    score = int(claim.get("fact_checkability_score", 1))
    category = claim.get("editorial_category", "rhetoric")

    if queue == "fact_check":
        status = "approved_for_check"
        verifiability = "checkable"
    elif queue == "review":
        status = "queued_for_review"
        verifiability = "partially_checkable"
    else:
        status = "archived_rhetoric"
        verifiability = "not_checkable"

    row = {
        "claim_text":             article["title"],
        "atomic_claim":           text,
        "speaker":                speaker,
        "role":                   role,
        "party":                  party,
        "source_name":            source,
        "source_url":             article.get("link", ""),
        "publication_date":       article.get("pub_date", "unknown"),
        "fetched_at":             fetched_at,
        "editorial_category":     category,
        "claim_type":             claim.get("claim_type", ""),
        "verifiability_status":   verifiability,
        "fact_checkability_score": score,
        "evidence_target":        claim.get("evidence_target") or "",
        "numeric_flag":           claim.get("numeric_flag", False),
        "legal_flag":             claim.get("legal_flag", False),
        "comparison_flag":        claim.get("comparison_flag", False),
        "timeframe_present":      claim.get("timeframe_present", False),
        "needs_human_review":     claim.get("needs_human_review", False),
        "rejection_reason":       claim.get("rejection_reason") or "",
        "status":                 status,
        "added_by":               "collector",
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)

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
    print("  Malta Political Claims - Collector v2")
    print("  Run time     : {}".format(fetched_at))
    print("  Looking back : {} hours".format(LOOKBACK_HRS))
    print("  Data folder  : {}".format(DATA_DIR))
    print("=" * 60)

    print("\n[Step 1] Fetching RSS feeds...")
    all_articles = []
    for feed in RSS_FEEDS:
        articles = fetch_feed(feed, cutoff)
        all_articles.extend(articles)

    print("\n  Total articles: {}".format(len(all_articles)))

    if not all_articles:
        print("  No articles found.")
        sys.exit(0)

    print("\n[Step 2] Extracting and classifying claims...")
    seen = load_existing()
    counts = {"fact_check": 0, "review": 0, "rhetoric": 0, "excluded": 0}

    for i, article in enumerate(all_articles):
        claims = process_article(client, article)

        for claim in claims:
            result = save_claim(claim, article, fetched_at, seen)
            if result:
                counts[result] += 1
                if result == "fact_check":
                    print("  [FC] {} - {}...".format(
                        article["source_name"], claim.get("atomic_claim","")[:70]))
            else:
                counts["excluded"] += 1

        if (i + 1) % 5 == 0 and i + 1 < len(all_articles):
            time.sleep(8)

    total = counts["fact_check"] + counts["review"] + counts["rhetoric"]
    print("\n" + "=" * 60)
    print("  Articles processed   : {}".format(len(all_articles)))
    print("  Fact-Check Queue     : +{}".format(counts["fact_check"]))
    print("  Review Queue         : +{}".format(counts["review"]))
    print("  Rhetoric Archive     : +{}".format(counts["rhetoric"]))
    print("  Excluded (proposals) : {}".format(counts["excluded"]))
    print("  Total saved          : {}".format(total))
    print("=" * 60)


if __name__ == "__main__":
    main()
