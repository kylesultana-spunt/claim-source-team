"""Analyser module — REMOVED.

This module used to classify claims into three queues (fact_check_queue,
review_queue, rhetoric_archive). That flow was replaced by editor-driven
triage in the admin UI: every extracted claim now lands in
claims_raw.csv and a human decides whether to send it for verification
or dismiss it.

This file is kept only so old imports produce a clear error instead of
a mysterious one. Do not re-use this name.
"""
raise ImportError(
    "spunt.analyser was removed — classification is now done by the "
    "editor in the admin UI (claims_raw.csv → sent_to_verify.csv)."
)
