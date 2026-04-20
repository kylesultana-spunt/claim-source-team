"""Central schema definitions.

Keep these column lists in exact lock-step with what the Cloudflare Pages
frontend reads from /data/*.csv. Adding columns is fine; reordering or
renaming the existing ones will break the site.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


# ---- inbox.csv -------------------------------------------------------------
INBOX_COLS = [
    "raw_statement",
    "speaker",
    "party",
    "role",
    "source_name",
    "source_url",
    "source_note",
    "publication_date",
    "collected_at",
    "topic",
    "processed",
]


@dataclass
class InboxRow:
    raw_statement: str
    source_name: str
    source_url: str
    speaker: str = "unknown"
    party: str = "unknown"
    role: str = ""
    source_note: str = ""
    publication_date: str = "unknown"
    collected_at: str = ""
    topic: str = "rss"
    processed: str = ""  # "" = pending, "done" = extracted

    def to_row(self) -> dict:
        if not self.collected_at:
            self.collected_at = utc_stamp()
        return {k: getattr(self, k) for k in INBOX_COLS}


# ---- queue CSVs (rhetoric_archive, review_queue, fact_check_queue) ---------
QUEUE_COLS = [
    "claim_text",
    "atomic_claim",
    "speaker",
    "role",
    "party",
    "source_name",
    "source_url",
    "publication_date",
    "fetched_at",
    "claim_type",          # statistical|comparative|policy|legal|administrative|historical|rhetorical|opinion
    "verifiability_status", # checkable|partially_checkable|not_checkable
    "fact_checkability_score",  # 1..5
    "evidence_target",      # free-text: where a fact-checker would look
    "numeric_flag",
    "legal_flag",
    "comparison_flag",
    "timeframe_present",
    "needs_human_review",
    "rejection_reason",
    "status",
]

# Status values the frontend expects
STATUS_APPROVED = "approved_for_check"
STATUS_QUEUED_REVIEW = "queued_for_review"
STATUS_ARCHIVED_RHETORIC = "archived_rhetoric"


@dataclass
class QueueRow:
    claim_text: str
    atomic_claim: str
    source_name: str
    source_url: str
    speaker: str = "unknown"
    role: str = ""
    party: str = "unknown"
    publication_date: str = "unknown"
    fetched_at: str = ""
    claim_type: str = ""
    verifiability_status: str = ""
    fact_checkability_score: int = 0
    evidence_target: str = ""
    numeric_flag: bool = False
    legal_flag: bool = False
    comparison_flag: bool = False
    timeframe_present: bool = False
    needs_human_review: bool = False
    rejection_reason: str = ""
    status: str = ""

    def to_row(self) -> dict:
        if not self.fetched_at:
            self.fetched_at = utc_stamp()
        d = asdict(self)
        # CSV-friendly boolean rendering that matches the existing files
        for k in ("numeric_flag", "legal_flag", "comparison_flag",
                  "timeframe_present", "needs_human_review"):
            d[k] = "TRUE" if d[k] else "FALSE"
        return {c: d[c] for c in QUEUE_COLS}


# ---- verdicts.csv (new) ----------------------------------------------------
VERDICT_COLS = [
    "atomic_claim",
    "speaker",
    "party",
    "source_url",
    "publication_date",
    "verdict",              # true|mostly_true|mixed|mostly_false|false|unverifiable
    "confidence",           # 1..5
    "summary",              # 2-3 sentence explanation
    "evidence",             # JSON list of {title, url, accessed, quote}
    "checked_at",
    "model",                # e.g. claude-sonnet-4-6
    "requires_review",      # TRUE for low-confidence or contested findings
]


@dataclass
class VerdictRow:
    atomic_claim: str
    speaker: str
    party: str
    source_url: str
    publication_date: str
    verdict: str
    confidence: int
    summary: str
    evidence: str  # JSON-encoded
    model: str
    requires_review: bool = False
    checked_at: str = ""

    def to_row(self) -> dict:
        if not self.checked_at:
            self.checked_at = utc_stamp()
        d = asdict(self)
        d["requires_review"] = "TRUE" if d["requires_review"] else "FALSE"
        return {c: d[c] for c in VERDICT_COLS}


def utc_stamp() -> str:
    """YYYY-MM-DD HH:MM UTC — matches the existing CSV timestamp format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
