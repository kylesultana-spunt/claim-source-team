"""Automated verdict generator.

Reads sent_to_verify.csv, finds rows whose status == "pending" (i.e.
the editor sent them for verification but we haven't run the model yet),
calls the verdict model with the web_search tool to gather **primary**
sources, then updates the same row in-place with verdict fields.

Note on file layout
-------------------
We no longer keep a separate verdicts.csv. sent_to_verify.csv has both
the claim fields and the verdict fields. A row's `status` column tells
you where it is in the process:
    pending    — editor approved it, model hasn't run yet
    verdicted  — model succeeded, verdict/confidence/summary populated
    failed     — model tried and gave up (rare; usually rate-limit or
                 repeated API errors)

The `requires_review` flag tells the public site to render a "pending
editorial review" badge when confidence <= 3 or evidence is thin. That
gives editors a kill-switch without blocking throughput.

Web-search tool note
--------------------
Anthropic's hosted `web_search_20250305` server tool is passed to the
model here. If it's unavailable in your account tier, swap in a custom
tool that calls e.g. Brave/Bing search and return the results as
tool_result blocks.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List

from .llm import MODEL_VERDICT, chat_json
from .schema import (
    VERIFICATION_COLS, STATUS_FAILED, STATUS_VERDICTED, utc_stamp,
)
from .storage import read_csv, write_csv_atomic

log = logging.getLogger("spunt.verdict")


WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 2,
}

# Pause between verdict calls to stay under Anthropic rate limits.
# Tier 1 allows 30K input tokens/min, and a single verdict call (system
# prompt + web_search round-trips) can easily consume the full minute-budget
# on its own. We wait long enough for the per-minute window to fully reset
# before starting the next verdict. If you upgrade to Tier 2, this can drop
# to ~15s.
INTER_CLAIM_PAUSE_SEC = 75.0


def _load_prompt(prompts_dir: Path) -> str:
    return (prompts_dir / "verdict.md").read_text(encoding="utf-8")


def _render_user(row: Dict) -> str:
    return (
        f"atomic_claim: {row['atomic_claim']}\n"
        f"speaker: {row.get('speaker','unknown')} "
        f"({row.get('role','')}, {row.get('party','')})\n"
        f"publication_date: {row.get('publication_date','unknown')}\n"
        f"source_url: {row.get('source_url','')}\n"
    )


def _apply_verdict(row: Dict, result: Dict, model: str) -> Dict:
    """Mutate the verification row in-place with the model's verdict fields."""
    confidence = int(result.get("confidence") or 1)
    evidence = result.get("evidence") or []
    # Auto-require review on low confidence or too-thin evidence, overriding
    # whatever the model returned.
    forced_review = confidence <= 3 or len(evidence) < 2
    requires_review = bool(result.get("requires_review")) or forced_review

    row["verdict"] = result.get("verdict", "unverifiable")
    row["confidence"] = str(confidence)
    row["summary"] = result.get("summary", "")
    row["evidence"] = json.dumps(evidence, ensure_ascii=False)
    row["requires_review"] = "TRUE" if requires_review else "FALSE"
    row["checked_at"] = utc_stamp()
    row["model"] = model
    row["status"] = STATUS_VERDICTED
    return row


def run(sent_to_verify_path: Path, prompts_dir: Path,
        max_per_run: int = 3) -> int:
    """Generate verdicts for up to `max_per_run` pending rows in sent_to_verify.csv.

    Returns the number of rows successfully verdicted.
    """
    system_prompt = _load_prompt(prompts_dir)
    rows: List[Dict] = read_csv(sent_to_verify_path)

    # Ensure every row has all the columns we'll be writing, even if the
    # existing file was saved by an older version of the code.
    for r in rows:
        for col in VERIFICATION_COLS:
            r.setdefault(col, "")

    processed = 0
    for row in rows:
        if processed >= max_per_run:
            log.info("verdict: hit max_per_run=%d, stopping for this run",
                     max_per_run)
            break
        if row.get("status", "").strip() != "pending":
            continue
        # Skip rows that somehow already have a verdict populated.
        if row.get("verdict"):
            continue

        # Be nice to the rate limiter on all but the first call.
        if processed > 0:
            time.sleep(INTER_CLAIM_PAUSE_SEC)

        try:
            result = chat_json(
                model=MODEL_VERDICT,
                system=system_prompt,
                user=_render_user(row),
                max_tokens=2000,
                tools=[WEB_SEARCH_TOOL],
            )
        except Exception as e:
            log.warning("verdict failed for claim %r: %s",
                        (row.get("atomic_claim", "") or "")[:80], e)
            row["status"] = STATUS_FAILED
            row["checked_at"] = utc_stamp()
            # Persist the failure so the next run doesn't hammer the same
            # row on the same failure.
            write_csv_atomic(sent_to_verify_path, VERIFICATION_COLS, rows)
            continue

        _apply_verdict(row, result, MODEL_VERDICT)
        processed += 1
        # Persist after each success so a mid-run crash never loses work
        # that's already paid for.
        write_csv_atomic(sent_to_verify_path, VERIFICATION_COLS, rows)

    return processed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    root = Path(__file__).resolve().parents[2]
    n = run(
        sent_to_verify_path=root / "data" / "sent_to_verify.csv",
        prompts_dir=root / "config" / "prompts",
    )
    print(f"verdict: wrote {n} new verdicts")
