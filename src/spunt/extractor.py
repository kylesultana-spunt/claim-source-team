"""Claim extractor.

For each inbox row where processed == "" (pending), call the LLM with
config/prompts/extract.md and write atomic claims to an **intermediate**
list. The analyser (next step) decides which queue each claim belongs to.

Dedup happens in TWO places:
    - Before LLM call: skip inbox rows whose URL is already fully processed.
    - After LLM call: skip claims that are fuzz-duplicate with anything
      already in inbox/review_queue/fact_check_queue/rhetoric_archive.

The extractor writes a temporary `pending_claims.csv` that the analyser
consumes. This separation means an LLM failure on the extract step
doesn't leave partial rows in the queues.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Iterable, List

from .dedup import is_near_duplicate
from .collector import load_sources
from .llm import MODEL_REASONING, chat_json
from .schema import INBOX_COLS, utc_stamp
from .storage import read_csv, write_csv_atomic

log = logging.getLogger("spunt.extractor")

PENDING_COLS = [
    "claim_text", "atomic_claim", "speaker", "role", "party",
    "source_name", "source_url", "publication_date", "fetched_at",
]


def _politicians_table(politicians: List[Dict]) -> str:
    lines = []
    for p in politicians:
        aliases = ", ".join(p.get("aliases", []))
        lines.append(f"- {p['name']} ({p['role']}, {p['party']}). Aliases: {aliases}")
    return "\n".join(lines)


def _load_prompt(path: Path, politicians: List[Dict]) -> str:
    raw = path.read_text(encoding="utf-8")
    return raw.replace("{politicians_table}", _politicians_table(politicians))


def _collect_existing_claims(data_dir: Path) -> List[str]:
    """All claim text already in any queue, for near-dupe comparison."""
    existing: List[str] = []
    for f in ("fact_check_queue.csv", "review_queue.csv", "rhetoric_archive.csv"):
        p = data_dir / f
        for row in read_csv(p):
            claim = row.get("atomic_claim") or row.get("claim_text")
            if claim:
                existing.append(claim)
    return existing


def run(inbox_path: Path, sources_path: Path, pending_path: Path,
        data_dir: Path, prompts_dir: Path) -> int:
    """Extract atomic claims from all pending inbox rows.

    Returns number of new atomic claims appended to pending_claims.csv.
    """
    _, politicians = load_sources(sources_path)
    system_prompt = _load_prompt(prompts_dir / "extract.md", politicians)

    inbox_rows = read_csv(inbox_path)
    existing_claims = _collect_existing_claims(data_dir)
    pending_existing = read_csv(pending_path)
    pending_claims = [r["atomic_claim"] for r in pending_existing if r.get("atomic_claim")]

    new_pending: List[Dict] = []
    dirty_inbox = False

    for row in inbox_rows:
        if row.get("processed"):
            continue
        body = row.get("raw_statement", "")
        if not body or len(body) < 200:
            row["processed"] = "skipped"
            dirty_inbox = True
            continue

        try:
            result = chat_json(
                model=MODEL_REASONING,
                system=system_prompt,
                user=body[:12000],  # hard cap; articles rarely longer
                max_tokens=2500,
            )
        except Exception as e:
            log.warning("extract failed for %s: %s", row.get("source_url"), e)
            continue

        for claim in result.get("claims", []):
            atomic = (claim.get("atomic_claim") or "").strip()
            if not atomic:
                continue
            # Near-dup against already-stored queues AND this pending batch
            if is_near_duplicate(atomic, existing_claims):
                continue
            if is_near_duplicate(atomic, pending_claims):
                continue

            new_pending.append({
                "claim_text": claim.get("claim_text") or atomic,
                "atomic_claim": atomic,
                "speaker": claim.get("speaker") or "unknown",
                "role": claim.get("role") or "",
                "party": claim.get("party") or "unknown",
                "source_name": row.get("source_name", ""),
                "source_url": row.get("source_url", ""),
                "publication_date": row.get("publication_date", "unknown"),
                "fetched_at": utc_stamp(),
            })
            pending_claims.append(atomic)

        row["processed"] = "done"
        dirty_inbox = True

    if dirty_inbox:
        write_csv_atomic(inbox_path, INBOX_COLS, inbox_rows)

    if new_pending:
        write_csv_atomic(pending_path, PENDING_COLS,
                         pending_existing + new_pending)
    return len(new_pending)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    root = Path(__file__).resolve().parents[2]
    n = run(
        inbox_path=root / "data" / "inbox.csv",
        sources_path=root / "config" / "sources.yml",
        pending_path=root / "data" / "pending_claims.csv",
        data_dir=root / "data",
        prompts_dir=root / "config" / "prompts",
    )
    print(f"extractor: appended {n} new atomic claims")
