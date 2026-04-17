#!/usr/bin/env python3
"""
Malta Political Claims - Analyser v2 (Multi-Agent Debate)

Two agents debate each claim per-claim before routing:

  Agent 1 — The Proposer
  Argues why this claim deserves to be fact-checked.
  Presents the claim, its source, speaker, and why it matters.

  Agent 2 — The Gatekeeper
  Challenges the claim. Asks: Is it specific enough? Is there a
  counter-claim? Is it a ranking, historical comparison, or opponent claim?
  Or is it just noise that should be archived?

  2-3 rounds of exchange. Then Gatekeeper gives final verdict:
    FACT_CHECK — strong, passes at least one editorial filter
    REVIEW     — borderline, needs human decision
    ARCHIVE    — fails all filters, not worth checking

Output CSVs gain two new columns:
  debate_summary   — condensed transcript of the debate
  passed_filter    — which filter(s) the claim passed
  counter_claim    — counter-claim text if found in debate
  analysed_at      — timestamp
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    import anthropic
except ImportError:
    print("ERROR: pip install anthropic")
    sys.exit(1)

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL   = "claude-haiku-4-5-20251001"

DATA_DIR = os.environ.get(
    "CLAIMS_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)

STAGING_FILE = os.path.join(DATA_DIR, "fact_check_queue.csv")
REVIEW_FILE  = os.path.join(DATA_DIR, "review_queue.csv")
ARCHIVE_FILE = os.path.join(DATA_DIR, "archive.csv")

CSV_COLUMNS = [
    "claim_text", "atomic_claim", "speaker", "role", "party",
    "source_name", "source_url", "publication_date", "fetched_at",
    "editorial_category", "claim_type", "verifiability_status",
    "fact_checkability_score", "evidence_target",
    "numeric_flag", "legal_flag", "comparison_flag", "timeframe_present",
    "needs_human_review", "rejection_reason", "status", "added_by",
    "counter_claim", "passed_filter", "debate_summary", "analysed_at",
]

# ── Agent 1: The Proposer ──────────────────────────────────────────────────

PROPOSER_SYSTEM = """You are a political journalist at a Maltese newsroom.
You have captured a claim from the news and you are arguing to your editor
why this claim deserves to enter the Fact-Check Queue.

Your job is to:
1. Present the claim clearly
2. Explain why it is specific and verifiable
3. Argue that it passes at least one of these editorial filters:
   - COUNTER_CLAIM: There is a known public dispute of this claim
   - RANKING: It places Malta in a position relative to other countries or EU members
   - HISTORICAL: It compares Malta's present situation to its past
   - OPPONENT: It is one political party making a verifiable claim about the other

Be concise. 3-4 sentences maximum per round.
Do not exaggerate. Stick to what the claim actually says.
If challenged fairly, concede ground but defend the core value of the claim."""

PROPOSER_OPENING = """Present this claim to your editor and argue why it deserves
to be fact-checked. Be concise and specific.

Claim: {atomic_claim}
Speaker: {speaker} ({party})
Source: {source_name}
Score: {score}/5
Evidence target: {evidence_target}

In 3-4 sentences, argue why this claim should enter the Fact-Check Queue."""

# ── Agent 2: The Gatekeeper ────────────────────────────────────────────────

GATEKEEPER_SYSTEM = """You are the editorial gatekeeper at a Maltese political newsroom.
Your job is to decide which claims enter the Fact-Check Queue and which go to Archive.

You are tough but fair. You reject noise, vague claims, and things that are
not actually verifiable. You keep claims that are genuinely useful for fact-checking.

HARD RULE — SPEAKER MUST BE A POLITICAL ACTOR:
Reject immediately if the speaker is a data institution such as Eurostat, NSO,
IMF, European Commission, Central Bank, TomTom, Transparency International,
or any statistics office or research body. These are sources used to VERIFY
claims — they are not political claim-makers. If the speaker is one of these,
verdict is ARCHIVE, filter is NONE.

You apply five editorial filters. A claim passes if it meets ANY ONE:

  COUNTER_CLAIM     — Is there a known named politician, institution or credible
                      source who publicly disputes this? An actual factual dispute,
                      not just a different opinion.

  RANKING           — Does it place Malta in a specific position relative to
                      other countries, EU members, or globally? Rankings are
                      always worth checking.

  HISTORICAL        — Does it compare Malta's current situation to a past figure,
                      trend, or benchmark? (e.g. debt fell from X to Y, rents
                      doubled since 2022, GDP grew faster than in 2019)

  OPPONENT          — Is it a verifiable claim made by one political party
                      about the policies, record, or statements of the other party?

  CONTRADICTS_PAST  — Does this new claim contradict or update a previously
                      accepted claim in our database? If a past claim said
                      "debt is at 47%" and this new claim says "debt is at 52%"
                      then both the old and new claim are worth examining together.
                      This filter also applies if a politician made a past claim
                      that new data now appears to disprove.

If the claim passes none of these filters: ARCHIVE it.
If the claim is borderline or you are genuinely unsure: REVIEW.
If the claim clearly passes at least one: FACT_CHECK.

Be direct. Challenge the proposer's arguments. Do not be persuaded by enthusiasm
alone — demand specificity. After round 2 or 3, give your final verdict."""

GATEKEEPER_OPENING = """The proposer is bringing you this claim for the Fact-Check Queue.
Read their argument and challenge it. Apply your five filters strictly.
After 2-3 rounds you will give a final verdict.

Proposer's argument: {proposer_opening}

{past_claims_context}

Challenge the claim. Ask the hard questions:
- Is the speaker a data institution? If so, reject immediately.
- Is it specific enough to verify?
- Does it pass any of your five filters?
- Does it contradict or update any past claim listed above?"""

VERDICT_PROMPT = """Based on the debate so far, give your final verdict now.

You must respond ONLY with valid JSON — no other text:

{{
  "verdict": "FACT_CHECK or REVIEW or ARCHIVE",
  "passed_filter": "COUNTER_CLAIM or RANKING or HISTORICAL or OPPONENT or CONTRADICTS_PAST or NONE",
  "counter_claim": "text of counter-claim if COUNTER_CLAIM filter passed, else null",
  "contradicts_past": "description of which past claim this contradicts or updates, else null",
  "reason": "one sentence explaining the verdict",
  "debate_summary": "2-3 sentence summary of the key points from the debate"
}}"""

# ── Deduplication ─────────────────────────────────────────────────────────

TOPIC_SIGNALS = {
    'DEBT':        ['debt', 'deficit', 'borrow', 'interest payment', 'billion', 'fiscal'],
    'HOUSING':     ['rent', 'housing', 'property', 'dwelling', 'affordable'],
    'TRAFFIC':     ['traffic', 'vehicle', 'congestion', 'transport', 'commute'],
    'CORRUPTION':  ['corruption', 'perceptions index', 'transparency international', 'cpi'],
    'EMPLOYMENT':  ['employment', 'unemployment', 'worker', 'job', 'labour market'],
    'ENVIRONMENT': ['environment', 'planning', 'construction', 'building permit', 'UNESCO'],
    'POLLS':       ['survey', 'poll', 'voting intention', 'trust rating', 'percentage points'],
    'GOVERNANCE':  ['constitution', 'auditor general', 'chief justice', 'legal notice', 'manifesto'],
    'ENERGY':      ['energy', 'electricity', 'fuel', 'gas price', 'subsidy'],
    'HEALTH':      ['hospital', 'mater dei', 'health', 'waiting'],
}

def get_topic(claim_text):
    text = claim_text.lower()
    topics = []
    for topic, signals in TOPIC_SIGNALS.items():
        if any(s in text for s in signals):
            topics.append(topic)
    return '|'.join(sorted(topics)) if topics else 'OTHER'

def is_duplicate(new_claim, new_speaker, past_claims):
    """
    Returns (is_dup, reason) where is_dup is True if a similar claim
    from the same speaker already exists in past_claims.
    """
    new_topic = get_topic(new_claim)
    new_spk   = (new_speaker or '').strip().lower()

    for past in past_claims:
        past_spk   = (past.get('speaker') or '').strip().lower()
        past_claim = past.get('atomic_claim', '')
        past_topic = get_topic(past_claim)

        # Must be same speaker
        if new_spk != past_spk:
            continue

        # Must be same topic
        if not new_topic or not past_topic:
            continue
        # Check topic overlap
        new_set  = set(new_topic.split('|'))
        past_set = set(past_topic.split('|'))
        if not new_set & past_set:
            continue

        # Same speaker, same topic — it's a duplicate
        return True, "Similar claim from {} on topic {} already exists: {}...".format(
            past_spk, '|'.join(new_set & past_set), past_claim[:60]
        )

    return False, None

# ── Deduplication ─────────────────────────────────────────────────────────

TOPIC_SIGNALS = {
    'DEBT':        ['debt', 'deficit', 'borrow', 'interest payment', 'fiscal'],
    'HOUSING':     ['rent', 'housing', 'property', 'dwelling', 'affordable'],
    'TRAFFIC':     ['traffic', 'vehicle', 'congestion', 'transport'],
    'CORRUPTION':  ['corruption', 'perceptions index', 'transparency international'],
    'EMPLOYMENT':  ['employment', 'unemployment', 'worker', 'job vacancy'],
    'ENVIRONMENT': ['environment', 'planning', 'construction', 'building permit', 'UNESCO'],
    'POLLS':       ['survey', 'poll', 'voting intention', 'trust rating'],
    'GOVERNANCE':  ['constitution', 'auditor general', 'chief justice', 'legal notice'],
    'ENERGY':      ['energy', 'electricity', 'fuel', 'subsidy'],
    'HEALTH':      ['hospital', 'mater dei', 'waiting'],
}

def get_topic(claim_text):
    text = claim_text.lower()
    topics = []
    for topic, signals in TOPIC_SIGNALS.items():
        if any(s in text for s in signals):
            topics.append(topic)
    return set(topics)

def is_duplicate(new_claim, new_speaker, past_claims):
    """Returns (is_dup, reason). True if same speaker + topic already in past_claims."""
    new_topics = get_topic(new_claim)
    new_spk = (new_speaker or '').strip().lower()
    for past in past_claims:
        past_spk = (past.get('speaker') or '').strip().lower()
        if new_spk != past_spk:
            continue
        past_topics = get_topic(past.get('atomic_claim', ''))
        overlap = new_topics & past_topics
        if overlap:
            return True, "Similar {} claim from {} already exists: {}...".format(
                '|'.join(overlap), past_spk, past.get('atomic_claim', '')[:60]
            )
    return False, None

# ── Past claims context ───────────────────────────────────────────────────

def load_past_claims(limit=200):
    """Load recent accepted claims from the fact_check_queue for context."""
    rows = load_csv(STAGING_FILE)
    # Only return analysed claims (have analysed_at set)
    past = [r for r in rows if r.get("analysed_at") and r.get("atomic_claim")]
    # Return most recent N claims
    return past[-limit:]

def find_related_past_claims(new_claim, past_claims, max_results=5):
    """
    Find past claims on the same topic as the new claim.
    Uses simple keyword overlap — cheap and fast, no API call needed.
    """
    new_words = set(new_claim.lower().split())
    # Remove common stop words
    stopwords = {"the", "a", "an", "is", "are", "was", "were", "of", "in",
                 "to", "and", "or", "for", "that", "this", "with", "by",
                 "at", "from", "has", "have", "had", "it", "its", "on",
                 "as", "be", "been", "will", "would", "could", "should",
                 "per", "than", "more", "less", "not", "no", "but"}
    new_words -= stopwords

    scored = []
    for row in past_claims:
        past_text = row.get("atomic_claim", "").lower()
        past_words = set(past_text.split()) - stopwords
        overlap = len(new_words & past_words)
        if overlap >= 2:  # at least 2 meaningful words in common
            scored.append((overlap, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:max_results]]

def format_past_claims_context(related):
    """Format related past claims as context for the Gatekeeper."""
    if not related:
        return "No related past claims found in the database."
    lines = ["Related past claims already in the database:"]
    for r in related:
        lines.append("  - [{}] {} — {} ({})".format(
            r.get("publication_date", "?"),
            r.get("atomic_claim", "")[:100],
            r.get("speaker", "?"),
            r.get("passed_filter", "?"),
        ))
    return "\n".join(lines)

# ── Debate engine ──────────────────────────────────────────────────────────

def run_debate(client, row, rounds=2, past_claims_context=""):
    """
    Run a multi-agent debate between Proposer and Gatekeeper.
    Returns a verdict dict.
    past_claims_context: formatted string of related past claims for Gatekeeper.
    """
    atomic    = row.get("atomic_claim", "").strip()
    speaker   = row.get("speaker", "unknown")
    party     = row.get("party", "")
    source    = row.get("source_name", "")
    score     = row.get("fact_checkability_score", "")
    evidence  = row.get("evidence_target", "")

    # --- Round 0: Proposer opens ---
    proposer_prompt = PROPOSER_OPENING.format(
        atomic_claim=atomic,
        speaker=speaker,
        party=party,
        source_name=source,
        score=score,
        evidence_target=evidence,
    )

    try:
        proposer_resp = client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=PROPOSER_SYSTEM,
            messages=[{"role": "user", "content": proposer_prompt}],
        )
        proposer_opening = "".join(
            b.text for b in proposer_resp.content if hasattr(b, "text")
        ).strip()
    except Exception as e:
        return _fallback_verdict(str(e))

    # Build conversation histories for each agent
    gatekeeper_history = [
        {
            "role": "user",
            "content": GATEKEEPER_OPENING.format(
                proposer_opening=proposer_opening,
                past_claims_context=past_claims_context,
            )
        }
    ]
    proposer_history = [
        {"role": "user",  "content": proposer_prompt},
        {"role": "assistant", "content": proposer_opening},
    ]

    # --- Debate rounds ---
    for round_num in range(rounds):
        # Gatekeeper challenges
        try:
            gk_resp = client.messages.create(
                model=MODEL,
                max_tokens=300,
                system=GATEKEEPER_SYSTEM,
                messages=gatekeeper_history,
            )
            gk_text = "".join(
                b.text for b in gk_resp.content if hasattr(b, "text")
            ).strip()
            gatekeeper_history.append({"role": "assistant", "content": gk_text})
        except Exception as e:
            return _fallback_verdict(str(e))

        time.sleep(1)

        # Proposer responds (except last round)
        if round_num < rounds - 1:
            proposer_history.append({
                "role": "user",
                "content": "The gatekeeper said: {}\n\nRespond and defend the claim in 2-3 sentences.".format(gk_text)
            })
            try:
                pr_resp = client.messages.create(
                    model=MODEL,
                    max_tokens=250,
                    system=PROPOSER_SYSTEM,
                    messages=proposer_history,
                )
                pr_text = "".join(
                    b.text for b in pr_resp.content if hasattr(b, "text")
                ).strip()
                proposer_history.append({"role": "assistant", "content": pr_text})
                gatekeeper_history.append({
                    "role": "user",
                    "content": "The proposer responds: {}\n\nContinue your assessment.".format(pr_text)
                })
            except Exception as e:
                return _fallback_verdict(str(e))

            time.sleep(1)

    # --- Final verdict from Gatekeeper ---
    gatekeeper_history.append({
        "role": "user",
        "content": VERDICT_PROMPT
    })
    try:
        verdict_resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=GATEKEEPER_SYSTEM,
            messages=gatekeeper_history,
        )
        verdict_raw = "".join(
            b.text for b in verdict_resp.content if hasattr(b, "text")
        ).strip()
        start = verdict_raw.find("{")
        end   = verdict_raw.rfind("}") + 1
        if start == -1:
            return _fallback_verdict("No JSON in verdict")
        return json.loads(verdict_raw[start:end])
    except Exception as e:
        return _fallback_verdict(str(e))


def _fallback_verdict(error):
    return {
        "verdict": "REVIEW",
        "passed_filter": "NONE",
        "counter_claim": None,
        "reason": "Debate error: {}".format(error[:80]),
        "debate_summary": "Debate could not complete due to an error.",
    }

# ── CSV helpers ────────────────────────────────────────────────────────────

def load_csv(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []

def append_row(path, row):
    exists = os.path.exists(path)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)

def rewrite_csv(path, rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

# ── Main ───────────────────────────────────────────────────────────────────

# Data institutions are sources not speakers — skip debate, archive immediately
DATA_INSTITUTIONS = {
    "nso", "eurostat", "european commission", "imf", "world bank",
    "central bank of malta", "central bank", "planning authority",
    "housing authority", "electoral commission", "tomtom", "inrix",
    "transparency international", "focuseconomics", "allianz trade",
    "kpmg", "pwc", "oecd", "un ", "united nations", "who",
    "european environment agency", "eea", "era", "transport malta",
    "infrastructure malta", "scope ratings", "fitch", "moody",
    "national statistics office", "statistics office",
    "global property guide", "investropa", "amphora media",
    "wikipedia", "grokipedia", "reference", "research institution",
    "news outlet", "international institution", "financial institution",
    "academic institution",
}

def is_data_institution(speaker):
    s = (speaker or "").strip().lower()
    return any(inst in s for inst in DATA_INSTITUTIONS)


def main():
    if not API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    client      = anthropic.Anthropic(api_key=API_KEY)
    analysed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print("=" * 60)
    print("  Malta Political Claims - Analyser v2 (Debate Mode)")
    print("  Run time : {}".format(analysed_at))
    print("  Staging  : {}".format(STAGING_FILE))
    print("=" * 60)

    staging_rows = load_csv(STAGING_FILE)
    if not staging_rows:
        print("\n  No claims in staging queue.")
        sys.exit(0)

    # Only process rows not yet analysed
    pending = [r for r in staging_rows if not r.get("analysed_at")]

    print("\n  Total in staging : {}".format(len(staging_rows)))
    print("  Pending debate   : {}".format(len(pending)))

    if not pending:
        print("\n  All claims already analysed.")
        sys.exit(0)

    counts = {"fact_check": 0, "review": 0, "archive": 0}
    debated_claims = set()

    print("\n[Running debates...]\n")

    for i, row in enumerate(pending):
        atomic  = row.get("atomic_claim", "").strip()
        speaker = row.get("speaker", "")
        print("  [{}/{}] {}...".format(i+1, len(pending), atomic[:60]))

        # Fast-path: data institutions are sources not speakers — archive immediately
        if is_data_institution(speaker):
            row["counter_claim"]   = ""
            row["passed_filter"]   = "NONE"
            row["debate_summary"]  = "Speaker is a data institution not a political actor. Data institutions are sources used to verify claims — they do not make political claims."
            row["analysed_at"]     = analysed_at
            row["status"]          = "archived_by_analyser"
            row["needs_human_review"] = "FALSE"
            row["rejection_reason"] = "Speaker is {} — a data source not a political actor".format(speaker)
            append_row(ARCHIVE_FILE, row)
            debated_claims.add(atomic.lower())
            counts["archive"] += 1
            print("     → ARCHIVE [data institution: {}]".format(speaker))
            continue

        verdict = run_debate(client, row, rounds=2)

        dest           = verdict.get("verdict", "REVIEW").upper()
        passed_filter  = verdict.get("passed_filter", "NONE")
        counter_claim  = verdict.get("counter_claim") or ""
        reason         = verdict.get("reason", "")
        debate_summary = verdict.get("debate_summary", "")

        # Map verdict to destination
        if dest == "FACT_CHECK":
            destination = "fact_check"
        elif dest == "ARCHIVE":
            destination = "archive"
        else:
            destination = "review"

        # Update row
        row["counter_claim"]   = counter_claim
        row["passed_filter"]   = passed_filter
        row["debate_summary"]  = debate_summary
        row["analysed_at"]     = analysed_at
        row["rejection_reason"] = reason if destination != "fact_check" else ""

        if destination == "fact_check":
            row["status"] = "approved_for_check"
            row["needs_human_review"] = "FALSE"
            append_row(STAGING_FILE, row)
            print("     → FACT_CHECK [{}] {}".format(passed_filter, reason[:50]))
        elif destination == "review":
            row["status"] = "queued_for_review"
            row["needs_human_review"] = "TRUE"
            append_row(REVIEW_FILE, row)
            print("     → REVIEW: {}".format(reason[:60]))
        else:
            row["status"] = "archived_by_analyser"
            row["needs_human_review"] = "FALSE"
            append_row(ARCHIVE_FILE, row)
            print("     → ARCHIVE: {}".format(reason[:60]))

        counts[destination] += 1
        debated_claims.add(atomic.lower())

        # Pause every 3 claims — debate is API-heavy
        if (i + 1) % 3 == 0 and i + 1 < len(pending):
            print("     [pausing 15s to respect rate limits]")
            time.sleep(15)

    # Remove debated rows from staging (keep already-analysed ones)
    remaining = [r for r in staging_rows
                 if r.get("atomic_claim", "").strip().lower()
                 not in debated_claims]
    rewrite_csv(STAGING_FILE, remaining)

    total = sum(counts.values())
    print("\n" + "=" * 60)
    print("  Claims debated     : {}".format(len(pending)))
    print("  → Fact-Check Queue : {}".format(counts["fact_check"]))
    print("  → Review Queue     : {}".format(counts["review"]))
    print("  → Archive          : {}".format(counts["archive"]))
    print("  Total routed       : {}".format(total))
    print("=" * 60)


if __name__ == "__main__":
    main()
