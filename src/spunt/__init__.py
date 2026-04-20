"""spunt.mt fact-check pipeline.

Modules:
    schema    - central column definitions + row factories
    storage   - atomic CSV read/write
    dedup     - claim normalization + near-dupe detection
    llm       - thin Anthropic client wrapper
    collector - RSS discovery + article fetching
    extractor - LLM atomic-claim extraction from article text
    analyser  - classifies claims and routes to the correct queue
    verdict   - automated fact-checking with cited web evidence
    cli       - `python -m spunt {collect|extract|analyse|verdict|all}`
"""
__version__ = "0.1.0"
