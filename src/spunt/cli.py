"""`python -m spunt <command>` entry point.

Commands:
    collect  - pull RSS feeds, fetch new articles into inbox.csv
    extract  - turn inbox articles into atomic claims in claims_raw.csv
    verdict  - run the web-search-backed verifier over the "pending" rows
               in sent_to_verify.csv and write verdict fields back in-place
    ingest   - collect + extract. The twice-daily schedule uses this.
               Editors triage newly-extracted claims via the admin portal.
    all      - collect + extract + verdict. Mainly useful for debugging;
               the normal live flow is `ingest` → manual triage in admin
               → `verdict`.

Data layout (two user-facing CSVs + one internal staging file):
    data/inbox.csv             — internal staging (articles -> extractor)
    data/claims_raw.csv        — every extracted claim. The admin's
                                 "Claims Raw" tab renders this. Editors
                                 select rows to send for verification.
    data/sent_to_verify.csv    — claims the editor approved. Verdict
                                 fields are appended to each row as the
                                 verifier runs against it.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from . import collector, extractor, verdict
from . import migrate as _migrate


def _paths(root: Path):
    return {
        "inbox":          root / "data" / "inbox.csv",
        "claims_raw":     root / "data" / "claims_raw.csv",
        "sent_to_verify": root / "data" / "sent_to_verify.csv",
        "sources":        root / "config" / "sources.yml",
        "prompts":        root / "config" / "prompts",
        "data_dir":       root / "data",
    }


def cmd_collect(p) -> None:
    n = collector.run(p["inbox"], p["sources"])
    print(f"collect: +{n} articles")


def cmd_extract(p) -> None:
    n = extractor.run(p["inbox"], p["sources"], p["claims_raw"],
                      p["data_dir"], p["prompts"])
    print(f"extract: +{n} atomic claims")


def cmd_verdict(p) -> None:
    n = verdict.run(p["sent_to_verify"], p["prompts"])
    print(f"verdict: +{n} verdicts")


def main() -> None:
    parser = argparse.ArgumentParser(prog="spunt")
    parser.add_argument("command",
                        choices=["collect", "extract", "verdict",
                                 "ingest", "all"])
    parser.add_argument("--root", default=".",
                        help="Project root (contains data/ and config/)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    root = Path(args.root).resolve()
    p = _paths(root)

    # Ensure the two user-facing CSVs exist with their expected headers.
    # No-op if they already exist.
    _migrate.ensure_fresh_files(p["data_dir"])

    if args.command == "collect":
        cmd_collect(p)
    elif args.command == "extract":
        cmd_extract(p)
    elif args.command == "verdict":
        cmd_verdict(p)
    elif args.command == "ingest":
        cmd_collect(p)
        cmd_extract(p)
    elif args.command == "all":
        cmd_collect(p)
        cmd_extract(p)
        cmd_verdict(p)


if __name__ == "__main__":
    main()
