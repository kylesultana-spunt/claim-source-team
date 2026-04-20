"""Claim deduplication.

Fixes a real issue in the current inbox: multiple near-identical rows for
the same underlying claim ("The Tourism Accommodation Regulations 2026
introduce..." appears 5+ times with slight wording changes).

Strategy:
    1. Normalize: lowercase, strip accents/punct, collapse whitespace,
       drop stopwords-lite, sort into a canonical token set.
    2. Hash the normalized form -> exact-dup O(1) lookup.
    3. For near-dupes, rapidfuzz token_set_ratio against claims from
       the same source URL (small candidate set, cheap).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable, Optional

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - tests skip if not installed
    fuzz = None


_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to",
    "for", "with", "by", "is", "was", "were", "be", "been", "being",
    "that", "which", "this", "these", "those", "it", "its",
    "said", "says", "stated", "claimed", "announced", "according",
    "mr", "ms", "mrs", "dr",
}

_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Canonical form for hashing: lowercase, no accents/punct, sorted tokens."""
    if not text:
        return ""
    # NFKD strips accented chars when combined with ascii-only filter
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    tokens = [t for t in text.split() if t and t not in _STOPWORDS]
    # sort so paraphrases with re-ordered clauses hash the same
    tokens.sort()
    return " ".join(tokens)


def fingerprint(text: str) -> str:
    """Stable 16-char fingerprint of the normalized text."""
    n = normalize(text)
    return hashlib.sha256(n.encode("utf-8")).hexdigest()[:16]


def _tokens(s: str) -> set:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = _PUNCT_RE.sub(" ", s.lower())
    return {t for t in s.split() if t and t not in _STOPWORDS}


def _jaccard(a: str, b: str) -> float:
    """Pure-Python fallback similarity — token-set Jaccard.
    Correlates well with rapidfuzz token_set_ratio for the kinds of
    duplicates we actually see (same claim, slightly different wording).
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_near_duplicate(candidate: str, existing: Iterable[str],
                      threshold: int = 88) -> Optional[str]:
    """Return the first `existing` claim that matches `candidate` above
    `threshold` (0-100), or None.

    Uses rapidfuzz token_set_ratio when available; falls back to Jaccard
    on token sets (converted to the same 0-100 scale) when not. Both
    handle word reordering + minor insertions which is exactly the
    pattern we see in the current inbox duplicates.
    """
    if fuzz is not None:
        for e in existing:
            if fuzz.token_set_ratio(candidate, e) >= threshold:
                return e
        return None

    # Fallback: Jaccard. Calibrated so threshold=88 maps ~= jaccard 0.62
    jacc_threshold = max(0.0, (threshold - 25) / 100.0)
    for e in existing:
        if _jaccard(candidate, e) >= jacc_threshold:
            return e
    return None
