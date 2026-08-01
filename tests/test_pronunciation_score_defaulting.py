"""Slice 6 (Phase 1 "stop actively miseducating"): the two buggy
_align_pronunciation call sites inside _feedback_impl (non-streaming) and the
/api/feedback/stream tail-processing branch computed a garbage
question-vs-answer pronunciation diff whenever a provider didn't supply a
real score. Both were deleted; a missing provider score must now stay None,
never be silently replaced with a computed number.

This test exercises enrich_feedback() directly (the shared normalisation
step both call sites fed into) rather than the full HTTP endpoints, since
_feedback_impl has heavy, non-DI'd dependencies (Supabase, rate limiter,
provider clients) unlike routers/pronunciation.py's configure() seam — see
test_pronunciation.py's docstring for that contrast.

Note: `main` is imported lazily inside each test (not at module scope).
main.py's module-level load_dotenv() call re-populates AZURE_SPEECH_KEY from
a local .env on first import, which raced with test_pronunciation.py's own
import-time env pop when both files were collected in the same pytest
session (whichever file's module body executed first "won", regardless of
declared pop order). Importing lazily, after this file's own pop, avoids
depending on cross-file import ordering entirely.

Run: pytest backend/tests/test_pronunciation_score_defaulting.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inspect

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)


def test_enrich_feedback_leaves_pronunciation_score_none_when_provider_omits_it():
    import main

    req = main.FeedbackRequest(question="Q", transcript="Une reponse quelconque.")
    fb: dict = {}

    result = main.enrich_feedback(fb, req)

    assert result["pronunciation"]["score"] is None


def test_feedback_impl_no_longer_calls_align_pronunciation():
    import main

    source = inspect.getsource(main._feedback_impl)
    assert "_align_pronunciation" not in source


def test_feedback_stream_no_longer_calls_align_pronunciation():
    import main

    source = inspect.getsource(main.feedback_stream)
    assert "_align_pronunciation" not in source


def test_align_pronunciation_still_defined_and_still_wired_into_the_pronunciation_router():
    import main

    # The function itself and its one legitimate call site (via
    # _configure_pronunciation, feeding routers/pronunciation.py) must remain
    # untouched — only the two buggy /api/feedback call sites were removed.
    assert callable(main._align_pronunciation)
    module_source = inspect.getsource(main)
    assert "_configure_pronunciation(" in module_source


if __name__ == "__main__":
    test_enrich_feedback_leaves_pronunciation_score_none_when_provider_omits_it()
    test_feedback_impl_no_longer_calls_align_pronunciation()
    test_feedback_stream_no_longer_calls_align_pronunciation()
    test_align_pronunciation_still_defined_and_still_wired_into_the_pronunciation_router()
    print("All test_pronunciation_score_defaulting tests passed.")
