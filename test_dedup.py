"""Tests for the dedup module.

These exercise the near-duplicate detection against claim pairs that
actually appear in the current inbox.csv — so if dedup regresses, we
know our real duplicates would slip through.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spunt.dedup import normalize, fingerprint, is_near_duplicate


def test_normalize_strips_punctuation_and_case():
    assert normalize("Hello, World!") == normalize("hello world")


def test_normalize_sorts_tokens():
    # Token sort makes paraphrases with reordered clauses match.
    a = normalize("Budget 2026 allocates €9.3 billion")
    b = normalize("€9.3 billion is allocated by Budget 2026")
    assert a == b


def test_fingerprint_is_stable():
    assert fingerprint("Hello, World!") == fingerprint("hello world")
    assert fingerprint("different claim") != fingerprint("hello world")


def test_near_duplicate_catches_real_inbox_pairs():
    # These pairs actually show up in the uploaded inbox.csv as separate rows.
    existing = [
        "The Tourism Accommodation Regulations 2026 introduce stricter rules "
        "across the short-term rental sector",
        "Malta ranks among the top three EU countries for the percentage of "
        "businesses affected by cyber incidents.",
        "28.7% of Maltese enterprises reported a cyber incident in 2023",
    ]
    candidates_that_should_dup = [
        "The Tourism Accommodation Regulations 2026 introduce stricter rules "
        "across the hotel and short-term rental sector",
        "Malta ranks among the top three EU countries for the percentage of "
        "businesses affected by cyber incidents",
    ]
    for c in candidates_that_should_dup:
        assert is_near_duplicate(c, existing) is not None, \
            f"should have flagged as dup: {c}"


def test_distinct_claims_are_not_duplicates():
    existing = ["Gozo's economy now exceeds €1 billion"]
    distinct = "Government revenue rose by €1.2 billion or 20% in the past year"
    assert is_near_duplicate(distinct, existing) is None
