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

You apply four editorial filters. A claim passes if it meets ANY ONE:
  COUNTER_CLAIM — Is there a known named politician, institution or credible
                  source who publicly disputes this? (not just a different opinion
                  but an actual factual dispute)
  RANKING       — Does it place Malta in a specific position relative to
                  other countries, EU members, or globally? Rankings are
                  always worth checking.
  HISTORICAL    — Does it compare Malta's current situation to a past figure,
                  trend, or benchmark? (e.g. debt fell from X to Y, rents
                  doubled since 2022, GDP grew faster than in 2019)
  OPPONENT      — Is it a verifiable claim made by one political party
                  about the policies, record, or statements of the other party?

If the claim passes none of these filters: ARCHIVE it.
If the claim is borderline or you are genuinely unsure: REVIEW.
If the claim clearly passes at least one: FACT_CHECK.

Be direct. Challenge the proposer's arguments. Do not be persuaded by enthusiasm
alone — demand specificity. After round 2 or 3, give your final verdict as:

VERDICT: FACT_CHECK | REVIEW | ARCHIVE
FILTER: COUNTER_CLAIM | RANKING | HISTORICAL | OPPONENT | NONE
COUNTER_CLAIM_TEXT: [text of counter-claim if found, or null]
REASON: [one sentence]"""

GATEKEEPER_OPENING = """The proposer is bringing you this claim for the Fact-Check Queue.
Read their argument and challenge it. Apply your four filters strictly.
After 2-3 rounds you will give a final verdict.

Proposer's argument: {proposer_opening}

Challenge the claim. Ask the hard questions. Is it specific enough?
Does it actually pass any of your four filters?"""

VERDICT_PROMPT = """Based on the debate so far, give your final verdict now.

You must respond ONLY with valid JSON — no other text:

{{
  "verdict": "FACT_CHECK or REVIEW or ARCHIVE",
  "passed_filter": "COUNTER_CLAIM or RANKING or HISTORICAL or OPPONENT or NONE",
  "counter_claim": "text of counter-claim if COUNTER_CLAIM filter passed, else null",
  "reason": "one sentence explaining the verdict",
  "debate_summary": "2-3 sentence summary of the key points from the debate"
}}"""

# ── Debate engine ──────────────────────────────────────────────────────────

def run_debate(client, row, rounds=2):
    """
    Run a multi-agent debate between Proposer and Gatekeeper.
    Returns a verdict dict.
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
                proposer_opening=proposer_opening
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
        atomic = row.get("atomic_claim", "").strip()
        print("  [{}/{}] {}...".format(i+1, len(pending), atomic[:60]))

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
