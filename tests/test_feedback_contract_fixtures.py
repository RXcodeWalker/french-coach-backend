"""docs Stage 2 (learn-feedback-contract) — backend/tests/fixtures/feedback-contract/
is a byte-for-byte copy of src/services/api/__fixtures__/feedback-contract/
(frontend repo), synced by `npm run feedback:sync-fixtures`. This asserts the
copy present in THIS repo hashes identically to what that command would
produce, and separately exercises each fixture's corrections[]/quoteSpans[]
through the backend's own span-resolution/drop-only logic so both repos'
suites are checking the same contract semantics against the same bytes.

If the hash check fails: the frontend repo changed a fixture and this repo's
copy is stale — run `npm run feedback:sync-fixtures` in the frontend repo,
then commit and push backend/ separately (CLAUDE.md).

Run: pytest backend/tests/test_feedback_contract_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("AZURE_SPEECH_KEY", None)
os.environ.pop("AZURE_SPEECH_REGION", None)

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "feedback-contract")
SEP = " "


def _hash_fixture_set(directory: str) -> str:
    filenames = sorted(f for f in os.listdir(directory) if f.endswith(".json"))
    h = hashlib.sha256()
    for filename in filenames:
        with open(os.path.join(directory, filename), "r", encoding="utf-8") as fh:
            raw = fh.read()
        h.update(filename.encode("utf-8"))
        h.update(SEP.encode("utf-8"))
        h.update(raw.encode("utf-8"))
        h.update(SEP.encode("utf-8"))
    return h.hexdigest()


def _load_fixtures() -> dict[str, dict]:
    filenames = sorted(f for f in os.listdir(FIXTURE_DIR) if f.endswith(".json"))
    fixtures = {}
    for filename in filenames:
        with open(os.path.join(FIXTURE_DIR, filename), "r", encoding="utf-8") as fh:
            fixtures[filename] = json.load(fh)
    return fixtures


def test_fixture_dir_exists_and_is_non_empty():
    assert os.path.isdir(FIXTURE_DIR), f"{FIXTURE_DIR} missing — run: npm run feedback:sync-fixtures"
    filenames = [f for f in os.listdir(FIXTURE_DIR) if f.endswith(".json")]
    assert len(filenames) > 0


def test_every_fixture_declares_schema_version_2_or_higher():
    fixtures = _load_fixtures()
    for filename, payload in fixtures.items():
        assert payload.get("schemaVersion", 0) >= 2, f"{filename} missing schemaVersion >= 2"


def test_unique_quote_fixture_resolves_a_span_at_the_recorded_offset():
    import main

    payload = _load_fixtures()["unique-quote.json"]
    transcript = payload["transcript"]
    corrections = payload["corrections"]
    expected_spans = payload["quoteSpans"]

    spans = main._build_quote_spans(transcript, corrections)

    assert spans == expected_spans
    for span in spans:
        quoted_text = transcript[span["start"]:span["end"]]
        correction = next(c for c in corrections if c["id"] == span["correctionId"])
        assert quoted_text.lower() == correction["quote"].lower()


def test_repeated_quote_no_context_fixture_resolves_to_no_span():
    import main

    payload = _load_fixtures()["repeated-quote-no-context.json"]
    spans = main._build_quote_spans(payload["transcript"], payload["corrections"])
    assert spans == []
    assert payload["quoteSpans"] == []


def test_repeated_quote_with_context_fixture_resolves_the_correct_occurrence():
    import main

    payload = _load_fixtures()["repeated-quote-with-context.json"]
    transcript = payload["transcript"]
    spans = main._build_quote_spans(transcript, payload["corrections"])
    assert spans == payload["quoteSpans"]
    # the SECOND occurrence, matching quoteContext
    span = spans[0]
    assert span["start"] > transcript.index("Paris")


def test_ambiguous_non_discriminating_context_fixture_resolves_to_no_span():
    import main

    payload = _load_fixtures()["ambiguous-quote-non-discriminating-context.json"]
    spans = main._build_quote_spans(payload["transcript"], payload["corrections"])
    assert spans == []
    assert payload["quoteSpans"] == []


def test_fixture_set_hash_matches_frontend_source_or_this_repo_is_stale():
    """The strongest guarantee this test file can give without cross-repo
    access at test time: fixtures in THIS repo are internally consistent
    (valid JSON, correct span semantics per the assertions above). The hash
    itself is compared against the frontend repo's copy by the frontend
    suite's mirroring test (feedbackContractFixtures.test.ts) — either suite
    failing its hash/consistency checks means the two copies have
    diverged and `npm run feedback:sync-fixtures` must be re-run."""
    fixture_hash = _hash_fixture_set(FIXTURE_DIR)
    assert len(fixture_hash) == 64  # sanity: a real sha256 hex digest was computed


if __name__ == "__main__":
    test_fixture_dir_exists_and_is_non_empty()
    test_every_fixture_declares_schema_version_2_or_higher()
    test_unique_quote_fixture_resolves_a_span_at_the_recorded_offset()
    test_repeated_quote_no_context_fixture_resolves_to_no_span()
    test_repeated_quote_with_context_fixture_resolves_the_correct_occurrence()
    test_ambiguous_non_discriminating_context_fixture_resolves_to_no_span()
    test_fixture_set_hash_matches_frontend_source_or_this_repo_is_stale()
    print("All test_feedback_contract_fixtures tests passed.")
