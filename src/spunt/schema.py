"""Central schema definitions.

Two CSVs now run the whole system:

    claims_raw.csv      — every extracted atomic claim, awaiting editor
                          triage. Columns = CLAIMS_COLS.
    sent_to_verify.csv  — claims the editor has promoted for automated
                          fact-checking. Verdict fields are appended
                          in-place (blank until verdict.py fills them).
                          Columns = VERIFICATION_COLS (CLAIMS_COLS +
                          verdict fields).

inbox.csv still exists as an internal staging file between the collector
and the extractor, but the admin UI doesn't need to show it.

Keep these column lists in exact lock-step with what the Cloudflare Pages
frontend reads. Adding columns at the END is safe; reordering or renaming
the existing ones will break the site.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone


# ---- inbox.csv (internal) --------------------------------------------------
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


# ---- claims.csv ------------------------------------------------------------
# One row per extracted atomic claim. This is what the "Claims" tab in the
# admin shows. The editor triages each row via the UI (verify / dismiss).
CLAIMS_COLS = [
    "claim_text",          # how it was written in the article
    "atomic_claim",        # self-contained, fact-check-ready sentence
    "speaker",
    "role",
    "party",
    "source_name",
    "source_url",
    "publication_date",
    "fetched_at",          # when this row was written to CSV
]


@dataclass
class ClaimRow:
    claim_text: str
    atomic_claim: str
    source_name: str
    source_url: str
    speaker: str = "unknown"
    role: str = ""
    party: str = "unknown"
    publication_date: str = "unknown"
    fetched_at: str = ""

    def to_row(self) -> dict:
        if not self.fetched_at:
            self.fetched_at = utc_stamp()
        d = asdict(self)
        return {c: d[c] for c in CLAIMS_COLS}


# ---- verification.csv ------------------------------------------------------
# Claims that have been sent for automated verification by the editor.
# Same columns as claims.csv, plus verdict fields that verdict.py fills in.
# A row has status="pending" until verdict.py either succeeds (status=
# "verdicted") or gives up (status="failed").
VERIFICATION_EXTRA_COLS = [
    "sent_for_verification_at",  # when the editor promoted this claim
    "status",                    # pending | verdicted | failed
    "verdict",                   # true | mostly_true | mixed |
                                 # mostly_false | false | unverifiable
    "confidence",                # 1..5 (blank until verdicted)
    "summary",                   # 2-3 sentence explanation
    "evidence",                  # JSON list of {title, url, accessed, quote}
    "requires_review",           # TRUE for low-confidence / thin evidence
    "checked_at",                # when verdict was produced (blank if pending)
    "model",                     # which model produced the verdict
]
VERIFICATION_COLS = CLAIMS_COLS + VERIFICATION_EXTRA_COLS

# Status values used in verification.csv.
STATUS_PENDING = "pending"
STATUS_VERDICTED = "verdicted"
STATUS_FAILED = "failed"


@dataclass
class VerificationRow:
    """A claim that's been sent for automated verification.

    Verdict fields default to blank so a newly-promoted claim starts as
    just a pending entry with no model output yet.
    """
    claim_text: str
    atomic_claim: str
    source_name: str
    source_url: str
    speaker: str = "unknown"
    role: str = ""
    party: str = "unknown"
    publication_date: str = "unknown"
    fetched_at: str = ""

    sent_for_verification_at: str = ""
    status: str = STATUS_PENDING
    verdict: str = ""
    confidence: str = ""          # stored as string for blank vs 0 clarity
    summary: str = ""
    evidence: str = ""            # JSON-encoded
    requires_review: str = "FALSE"
    checked_at: str = ""
    model: str = ""

    def to_row(self) -> dict:
        if not self.fetched_at:
            self.fetched_at = utc_stamp()
        if not self.sent_for_verification_at:
            self.sent_for_verification_at = utc_stamp()
        d = asdict(self)
        return {c: d[c] for c in VERIFICATION_COLS}


def utc_stamp() -> str:
    """YYYY-MM-DD HH:MM UTC — matches the existing CSV timestamp format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Legacy names kept for import compatibility. The analyser module still
# imports some of these; nothing else should use them for new code.
QUEUE_COLS = CLAIMS_COLS  # effectively deprecated — "queue" = claims now
VERDICT_COLS = VERIFICATION_COLS  # deprecated; use VERIFICATION_COLS
