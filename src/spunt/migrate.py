"""Ensure the two user-facing CSVs exist with the right header row.

Called once at the top of every CLI invocation. If claims_raw.csv or
sent_to_verify.csv is missing, create it with just the header row so
downstream code can always assume the files are there.

This module used to perform a data migration from the old multi-file
layout; we dropped that because the project switched to a clean-slate
reset instead of an in-place upgrade. Keeping the module around — with a
different responsibility — lets cli.py's call site stay stable and lets
us add more initialisation steps here later without touching cli.py.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .schema import CLAIMS_COLS, VERIFICATION_COLS
from .storage import write_csv_atomic

log = logging.getLogger("spunt.migrate")


def ensure_fresh_files(data_dir: Path) -> None:
    """Create claims_raw.csv and sent_to_verify.csv if they're missing.

    Idempotent — running this on a data directory that already has the
    two files does nothing. This is safe to call on every pipeline run.
    """
    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)

    raw_path = data_dir / "claims_raw.csv"
    if not raw_path.exists():
        write_csv_atomic(raw_path, CLAIMS_COLS, [])
        log.info("created empty %s", raw_path.name)

    ver_path = data_dir / "sent_to_verify.csv"
    if not ver_path.exists():
        write_csv_atomic(ver_path, VERIFICATION_COLS, [])
        log.info("created empty %s", ver_path.name)
