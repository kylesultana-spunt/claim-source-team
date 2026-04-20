"""Automated verdict generator.

Reads fact_check_queue.csv (rows with status=approved_for_check that don't
yet have a verdict), calls the verdict model with the web_search tool to
gather **primary** sources, then writes the structured verdict into
verdicts.csv.

Even though runs are automated end-to-end per the project decision, the
`requires_review` flag tells the Cloudflare frontend to render a "pending
editorial review" badge when confidence <= 3 or evidence is thin. That
gives editors a kill-switch without blocking throughput.

Web-search tool note
--------------------
Anthropic's hosted `web_search_20250305` server tool is passed to the model
here. If it's unavailable in your account tier, swap in a custom tool that
calls e.g. Brave/Bing search and return the results as tool_result blocks.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List

from .llm import MODEL_VERDICT, chat_json
from .schema import VERDICT_COLS, VerdictRow, utc_stamp
from .storage import read_csv, write_csv_atomic

log = logging.getLogger("spunt.verdict")


WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
}


def _load_prompt(prompts_dir: Path) -> str:
    return (prompts_dir / "verdict.md").read_text(encoding="utf-8")


def _already_verdicted(verdicts: List[Dict]) -> set:
    # Match on (atomic_claim, source_url) pair — same claim can legitimately
    # be made by multiple politicians in separate articles.
    return {(v.get("atomic_claim", ""), v.get("source_url", ""))
            for v in verdicts}


def _render_user(claim_row: Dict) -> str:
    return (
        f"atomic_claim: {claim_row['atomic_claim']}\n"
        f"speaker: {claim_row.get('speaker','unknown')} "
        f"({claim_row.get('role','')}, {claim_row.get('party','')})\n"
        f"publication_date: {claim_row.get('publication_date','unknown')}\n"
        f"evidence_target: {claim_row.get('evidence_target','')}\n"
        f"source_url: {claim_row.get('source_url','')}\n"
    )


def _verdict_to_row(claim_row: Dict, result: Dict, model: str) -> VerdictRow:
    confidence = int(result.get("confidence") or 1)
    # Auto-require review on low confidence or too-thin evidence, overriding
    # whatever the model returned.
    evidence = result.get("evidence") or []
    forced_review = confidence <= 3 or len(evidence) < 2
    requires_review = bool(result.get("requires_review")) or forced_review

    return VerdictRow(
        atomic_claim=claim_row["atomic_claim"],
        speaker=claim_row.get("speaker", "unknown"),
        party=claim_row.get("party", "unknown"),
        source_url=claim_row.get("source_url", ""),
        publication_date=claim_row.get("publication_date", "unknown"),
        verdict=result.get("verdict", "unverifiable"),
        confidence=confidence,
        summary=result.get("summary", ""),
        evidence=json.dumps(evidence, ensure_ascii=False),
        model=model,
        requires_review=requires_review,
        checked_at=utc_stamp(),
    )


def run(fact_check_path: Path, verdicts_path: Path, prompts_dir: Path,
        max_per_run: int = 20) -> int:
    """Generate verdicts for up to `max_per_run` unverdicted claims."""
    system_prompt = _load_prompt(prompts_dir)

    claims = read_csv(fact_check_path)
    verdicts = read_csv(verdicts_path)
    done = _already_verdicted(verdicts)

    new_verdicts: List[Dict] = []
    processed = 0

    for row in claims:
        if processed >= max_per_run:
            break
        key = (row.get("atomic_claim", ""), row.get("source_url", ""))
        if key in done:
            continue
        if row.get("status") != "approved_for_check":
            continue

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
                        row.get("atomic_claim", "")[:80], e)
            continue

        new_verdicts.append(_verdict_to_row(row, result, MODEL_VERDICT).to_row())
        processed += 1

    if new_verdicts:
        write_csv_atomic(verdicts_path, VERDICT_COLS,
                         verdicts + new_verdicts)
    return len(new_verdicts)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    root = Path(__file__).resolve().parents[2]
    n = run(
        fact_check_path=root / "data" / "fact_check_queue.csv",
        verdicts_path=root / "data" / "verdicts.csv",
        prompts_dir=root / "config" / "prompts",
    )
    print(f"verdict: wrote {n} new verdicts")
