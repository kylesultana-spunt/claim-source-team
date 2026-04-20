"""Atomic CSV read/write.

Why atomic? GitHub Actions commits whatever is in `data/` after the run.
If a pipeline step crashes mid-write, we don't want a half-written CSV to
be committed. Write to a temp file in the same directory, fsync, rename.
"""
from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path
from typing import Iterable, List, Dict


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv_atomic(path: Path, columns: List[str], rows: Iterable[Dict]) -> None:
    """Write rows to `path` atomically.

    `rows` may include extra keys; only `columns` are written (in order).
    Missing values are rendered as empty strings.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile on same dir so os.replace is atomic on the same FS.
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                w.writerow({c: _stringify(row.get(c, "")) for c in columns})
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def append_csv(path: Path, columns: List[str], new_rows: Iterable[Dict]) -> int:
    """Read existing + append new rows + atomic write. Returns #new rows."""
    existing = read_csv(path)
    new_list = list(new_rows)
    write_csv_atomic(path, columns, existing + new_list)
    return len(new_list)


def _stringify(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return str(v)
