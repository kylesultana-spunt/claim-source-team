"""`python -m spunt <command>` entry point.

Commands:
    collect  - pull RSS feeds, fetch new articles into inbox.csv
    extract  - turn inbox articles into pending atomic claims
    analyse  - (optional, off by default) AI-classify pending claims into
               fact_check / review / rhetoric queues. No longer part of the
               scheduled ingest; editors triage from pending_claims.csv via
               the admin portal instead.
    verdict  - generate automated verdicts for approved_for_check claims
    ingest   - collect + extract ONLY. Everything new lands in
               pending_claims.csv for a human to sort in the admin portal.
    all      - run every stage (collect + extract + analyse + verdict).
               Mainly for debugging; the normal live flow is
               ingest -> manual triage in admin -> verdict.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from . import collector, extractor, analyser, verdict


def _paths(root: Path):
    return {
        "inbox": root / "data" / "inbox.csv",
        "pending": root / "data" / "pending_claims.csv",
        "fact_check": root / "data" / "fact_check_queue.csv",
        "review": root / "data" / "review_queue.csv",
        "rhetoric": root / "data" / "rhetoric_archive.csv",
        "verdicts": root / "data" / "verdicts.csv",
        "sources": root / "config" / "sources.yml",
        "prompts": root / "config" / "prompts",
        "data_dir": root / "data",
    }


def cmd_collect(p) -> None:
    n = collector.run(p["inbox"], p["sources"])
    print(f"collect: +{n} articles")


def cmd_extract(p) -> None:
    n = extractor.run(p["inbox"], p["sources"], p["pending"],
                      p["data_dir"], p["prompts"])
    print(f"extract: +{n} atomic claims")


def cmd_analyse(p) -> None:
    summary = analyser.run(p["pending"], p["data_dir"], p["prompts"])
    print(f"analyse: {summary}")


def cmd_verdict(p) -> None:
    n = verdict.run(p["fact_check"], p["verdicts"], p["prompts"])
    print(f"verdict: +{n} verdicts")


def main() -> None:
    parser = argparse.ArgumentParser(prog="spunt")
    parser.add_argument("command",
                        choices=["collect", "extract", "analyse", "verdict",
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

    if args.command == "collect":
        cmd_collect(p)
    elif args.command == "extract":
        cmd_extract(p)
    elif args.command == "analyse":
        cmd_analyse(p)
    elif args.command == "verdict":
        cmd_verdict(p)
    elif args.command == "ingest":
        cmd_collect(p)
        cmd_extract(p)
        # Note: no analyser. Pending claims sit in pending_claims.csv for a
        # human editor to triage via the admin portal. This is deliberate —
        # the editor decides what gets fact-checked, not the model.
    elif args.command == "all":
        cmd_collect(p)
        cmd_extract(p)
        cmd_analyse(p)
        cmd_verdict(p)


if __name__ == "__main__":
    main()
