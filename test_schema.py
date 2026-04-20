"""Round-trip tests: writing with our schema produces exactly the columns
and values that the existing frontend reads.
"""
import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spunt.schema import (
    INBOX_COLS, QUEUE_COLS, VERDICT_COLS,
    InboxRow, QueueRow, VerdictRow,
    STATUS_APPROVED,
)
from spunt.storage import write_csv_atomic, read_csv


def test_inbox_row_columns(tmp_path):
    row = InboxRow(
        raw_statement="Abela said the budget is €9.3 billion.",
        source_name="Newsbook",
        source_url="https://example.com/a",
        speaker="Robert Abela",
        party="PL",
        role="Prime Minister",
        publication_date="2026-04-20",
    ).to_row()
    assert set(row.keys()) == set(INBOX_COLS)

    p = tmp_path / "inbox.csv"
    write_csv_atomic(p, INBOX_COLS, [row])
    back = read_csv(p)
    assert len(back) == 1
    assert back[0]["speaker"] == "Robert Abela"
    assert back[0]["raw_statement"].startswith("Abela said")


def test_queue_row_serializes_booleans_as_TRUE_FALSE(tmp_path):
    row = QueueRow(
        claim_text="x", atomic_claim="x",
        source_name="s", source_url="u",
        speaker="Robert Abela", role="PM", party="PL",
        publication_date="2026-04-20",
        claim_type="statistical",
        verifiability_status="checkable",
        fact_checkability_score=5,
        numeric_flag=True, legal_flag=False,
        comparison_flag=False, timeframe_present=True,
        needs_human_review=False, rejection_reason="",
        status=STATUS_APPROVED,
    ).to_row()
    assert row["numeric_flag"] == "TRUE"
    assert row["legal_flag"] == "FALSE"

    p = tmp_path / "q.csv"
    write_csv_atomic(p, QUEUE_COLS, [row])
    back = read_csv(p)
    assert back[0]["numeric_flag"] == "TRUE"
    assert back[0]["status"] == STATUS_APPROVED


def test_verdict_row_columns(tmp_path):
    row = VerdictRow(
        atomic_claim="x", speaker="Abela", party="PL",
        source_url="u", publication_date="2026-04-20",
        verdict="true", confidence=5, summary="ok",
        evidence='[{"title":"t","url":"u","accessed":"2026-04-20","quote":"q"}]',
        model="claude-opus-4-6",
        requires_review=False,
    ).to_row()
    assert set(row.keys()) == set(VERDICT_COLS)
    assert row["verdict"] == "true"
    assert row["requires_review"] == "FALSE"
