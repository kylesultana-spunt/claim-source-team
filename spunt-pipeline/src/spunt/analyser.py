"""Analyser / classifier.

Reads pending_claims.csv (produced by the extractor) and for each claim
calls the classify prompt. Based on the result, the claim is routed to
exactly one of:
    - fact_check_queue.csv  (approved_for_check, score >= 4)
    - review_queue.csv      (partially checkable, needs human review)
    - rhetoric_archive.csv  (not_checkable, with a rejection_reason)

After successful classification, the pending row is removed from
pending_claims.csv. If the LLM call fails, the row stays pending so the
next run can retry.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List

from .extractor import PENDING_COLS
from .llm import MODEL_REASONING, chat_json
from .schema import (
    QUEUE_COLS, QueueRow,
    STATUS_APPROVED, STATUS_ARCHIVED_RHETORIC, STATUS_QUEUED_REVIEW,
)
from .storage import read_csv, write_csv_atomic

log = logging.getLogger("spunt.analyser")


def _load_prompt(prompts_dir: Path) -> str:
    return (prompts_dir / "classify.md").read_text(encoding="utf-8")


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"true", "1", "yes"}


def _route(classification: Dict) -> str:
    verifiability = (classification.get("verifiability_status") or "").strip()
    score = int(classification.get("fact_checkability_score") or 0)

    if verifiability == "not_checkable":
        return STATUS_ARCHIVED_RHETORIC
    if verifiability == "checkable" and score >= 4:
        return STATUS_APPROVED
    return STATUS_QUEUED_REVIEW


def _build_queue_row(pending: Dict, cls: Dict, status: str) -> QueueRow:
    return QueueRow(
        claim_text=pending.get("claim_text", ""),
        atomic_claim=pending.get("atomic_claim", ""),
        speaker=pending.get("speaker", "unknown"),
        role=pending.get("role", ""),
        party=pending.get("party", "unknown"),
        source_name=pending.get("source_name", ""),
        source_url=pending.get("source_url", ""),
        publication_date=pending.get("publication_date", "unknown"),
        fetched_at=pending.get("fetched_at", ""),
        claim_type=cls.get("claim_type", ""),
        verifiability_status=cls.get("verifiability_status", ""),
        fact_checkability_score=int(cls.get("fact_checkability_score") or 0),
        evidence_target=cls.get("evidence_target", ""),
        numeric_flag=_to_bool(cls.get("numeric_flag")),
        legal_flag=_to_bool(cls.get("legal_flag")),
        comparison_flag=_to_bool(cls.get("comparison_flag")),
        timeframe_present=_to_bool(cls.get("timeframe_present")),
        needs_human_review=_to_bool(cls.get("needs_human_review"))
                           or status == STATUS_QUEUED_REVIEW,
        rejection_reason=cls.get("rejection_reason", "")
                         if status == STATUS_ARCHIVED_RHETORIC else "",
        status=status,
    )


def run(pending_path: Path, data_dir: Path, prompts_dir: Path) -> Dict[str, int]:
    system_prompt = _load_prompt(prompts_dir)

    pending = read_csv(pending_path)
    if not pending:
        return {"archived_rhetoric": 0, "queued_for_review": 0,
                "approved_for_check": 0, "remaining_pending": 0}

    # Load current queues once; append as we classify.
    queues: Dict[str, List[Dict]] = {
        STATUS_APPROVED: read_csv(data_dir / "fact_check_queue.csv"),
        STATUS_QUEUED_REVIEW: read_csv(data_dir / "review_queue.csv"),
        STATUS_ARCHIVED_RHETORIC: read_csv(data_dir / "rhetoric_archive.csv"),
    }
    counts = {k: 0 for k in queues}

    still_pending: List[Dict] = []
    for p in pending:
        user_msg = (
            f"atomic_claim: {p['atomic_claim']}\n"
            f"speaker: {p.get('speaker','unknown')}\n"
            f"role: {p.get('role','')}\n"
            f"party: {p.get('party','unknown')}\n"
            f"source: {p.get('source_name','')}\n"
        )
        try:
            cls = chat_json(model=MODEL_REASONING, system=system_prompt,
                            user=user_msg, max_tokens=800)
        except Exception as e:
            log.warning("classify failed for claim %r: %s",
                        p.get("atomic_claim", "")[:80], e)
            still_pending.append(p)
            continue

        status = _route(cls)
        row = _build_queue_row(p, cls, status).to_row()
        queues[status].append(row)
        counts[status] += 1

    # Atomic writes
    write_csv_atomic(data_dir / "fact_check_queue.csv", QUEUE_COLS,
                     queues[STATUS_APPROVED])
    write_csv_atomic(data_dir / "review_queue.csv", QUEUE_COLS,
                     queues[STATUS_QUEUED_REVIEW])
    write_csv_atomic(data_dir / "rhetoric_archive.csv", QUEUE_COLS,
                     queues[STATUS_ARCHIVED_RHETORIC])
    write_csv_atomic(pending_path, PENDING_COLS, still_pending)

    counts["remaining_pending"] = len(still_pending)
    return counts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    root = Path(__file__).resolve().parents[2]
    summary = run(
        pending_path=root / "data" / "pending_claims.csv",
        data_dir=root / "data",
        prompts_dir=root / "config" / "prompts",
    )
    print("analyser:", summary)
