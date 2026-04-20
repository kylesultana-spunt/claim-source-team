"""Claim extractor.

For each inbox row where processed == "" (pending), call the LLM with
config/prompts/extract.md and append atomic claims to claims_raw.csv.

Dedup happens in two places:
    - Before LLM call: skip inbox rows whose URL is already fully processed.
    - After LLM call: skip claims that are fuzz-duplicate with anything
      already in claims_raw.csv OR sent_to_verify.csv. This stops the
      same claim being extracted twice when the same story runs in two
      outlets.

claims_raw.csv is the single source of truth for new extracted claims —
an editor triages each row in the admin portal (select & send to verify,
or dismiss). Verdict generation reads sent_to_verify.csv, not this file.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List

from .dedup import is_near_duplicate
from .collector import load_sources
from .llm import MODEL_REASONING, chat_json
from .schema import CLAIMS_COLS, INBOX_COLS, utc_stamp
from .storage import read_csv, write_csv_atomic

log = logging.getLogger("spunt.extractor")


def _politicians_table(politicians: List[Dict]) -> str:
    lines = []
    for p in politicians:
        aliases = ", ".join(p.get("aliases", []))
        lines.append(f"- {p['name']} ({p['role']}, {p['party']}). Aliases: {aliases}")
    return "\n".join(lines)


def _load_prompt(path: Path, politicians: List[Dict]) -> str:
    raw = path.read_text(encoding="utf-8")
    return raw.replace("{politicians_table}", _politicians_table(politicians))


def _already_known_claims(data_dir: Path) -> List[str]:
    """Every atomic_claim currently in claims_raw.csv or sent_to_verify.csv.

    Used by the fuzzy duplicate filter so we don't extract the same claim
    twice when the same story breaks in two outlets.
    """
    existing: List[str] = []
    for fname in ("claims_raw.csv", "sent_to_verify.csv"):
        for row in read_csv(data_dir / fname):
            claim = row.get("atomic_claim") or row.get("claim_text")
            if claim:
                existing.append(claim)
    return existing


def run(inbox_path: Path, sources_path: Path, claims_path: Path,
        data_dir: Path, prompts_dir: Path) -> int:
    """Extract atomic claims from all pending inbox rows.

    Returns the number of new atomic claims appended to claims_raw.csv.
    """
    _, politicians = load_sources(sources_path)
    system_prompt = _load_prompt(prompts_dir / "extract.md", politicians)

    inbox_rows = read_csv(inbox_path)
    existing_claims = _already_known_claims(data_dir)
    existing_rows = read_csv(claims_path)
    seen_in_batch = [r["atomic_claim"] for r in existing_rows if r.get("atomic_claim")]

    new_claims: List[Dict] = []
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
            if is_near_duplicate(atomic, existing_claims):
                continue
            if is_near_duplicate(atomic, seen_in_batch):
                continue

            new_claims.append({
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
            seen_in_batch.append(atomic)

        row["processed"] = "done"
        dirty_inbox = True

    if dirty_inbox:
        write_csv_atomic(inbox_path, INBOX_COLS, inbox_rows)

    if new_claims:
        write_csv_atomic(claims_path, CLAIMS_COLS,
                         existing_rows + new_claims)
    return len(new_claims)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    root = Path(__file__).resolve().parents[2]
    n = run(
        inbox_path=root / "data" / "inbox.csv",
        sources_path=root / "config" / "sources.yml",
        claims_path=root / "data" / "claims_raw.csv",
        data_dir=root / "data",
        prompts_dir=root / "config" / "prompts",
    )
    print(f"extractor: appended {n} new atomic claims to claims_raw.csv")
