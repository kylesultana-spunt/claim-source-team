"""spunt.mt fact-check pipeline.

Modules:
    schema    - central column definitions + row factories
    storage   - atomic CSV read/write
    dedup     - claim normalization + near-dupe detection
    llm       - thin Anthropic client wrapper
    collector - RSS discovery + article fetching
    extractor - LLM atomic-claim extraction from article text
    verdict   - automated fact-checking with cited web evidence
    migrate   - ensure the two user-facing CSVs exist at startup
    cli       - `python -m spunt {collect|extract|verdict|ingest|all}`

Data layout:
    data/inbox.csv           — internal staging for the extractor
    data/claims_raw.csv      — all extracted claims, pending triage
    data/sent_to_verify.csv  — claims the editor sent for verification
                               (verdict fields are filled in-place)
"""
__version__ = "0.2.0"
