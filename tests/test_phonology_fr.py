"""Tests for services/phonology/rules.py (accent-analyzer plan §6, §17).

Negative cases are mandatory (plan §17): *bonne*, *personne*, *monotone*
must NOT be flagged nasal — these are exactly the words the old, deleted
main.py heuristic (`"on" in word.lower()`) got wrong, since none of them
contain the nasal vowel /ɔ̃/ (the "onn"/"onn"/"one" spellings denasalise).
This suite proves the new IPA-anchored rule doesn't repeat that mistake.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.phonology import rules


def _word(text: str, phonemes=None, error_type=None, offset_ms=None, duration_ms=None, near_boundary=False):
    return {
        "word": text,
        "errorType": error_type,
        "phonemes": phonemes,
        "offsetMs": offset_ms,
        "durationMs": duration_ms,
        "nearChunkBoundary": near_boundary,
    }


def _phoneme(symbol: str, accuracy):
    return {"phoneme": symbol, "accuracyScore": accuracy}


# ── Nasal vowels ──────────────────────────────────────────────────────────

def test_nasal_vowel_flagged_on_low_accuracy_ipa_trigger():
    words = [_word("vin", phonemes=[_phoneme("v", 92.0), _phoneme("ɛ̃", 20.0)])]
    findings = rules.evaluate(words)
    assert any(f["category"] == "nasalVowel" and f["word"] == "vin" for f in findings)


def test_nasal_vowel_not_flagged_when_accuracy_high():
    words = [_word("vin", phonemes=[_phoneme("v", 92.0), _phoneme("ɛ̃", 95.0)])]
    findings = rules.evaluate(words)
    assert not any(f["category"] == "nasalVowel" for f in findings)


def test_bonne_not_flagged_nasal():
    # "bonne" -> /bɔn/, oral vowel + n (geminate spelling denasalises) — the
    # old substring heuristic ("on" in "bonne") would have wrongly flagged
    # this. No /ɔ̃/ phoneme is present here, so the new rule cannot trigger.
    words = [_word("bonne", phonemes=[_phoneme("b", 90.0), _phoneme("ɔ", 88.0), _phoneme("n", 91.0)])]
    findings = rules.evaluate(words)
    assert not any(f["category"] == "nasalVowel" for f in findings)


def test_personne_not_flagged_nasal():
    words = [_word(
        "personne",
        phonemes=[_phoneme("p", 90.0), _phoneme("ɛ", 85.0), _phoneme("ʁ", 80.0), _phoneme("s", 88.0), _phoneme("ɔ", 87.0), _phoneme("n", 90.0)],
    )]
    findings = rules.evaluate(words)
    assert not any(f["category"] == "nasalVowel" for f in findings)


def test_monotone_not_flagged_nasal():
    words = [_word(
        "monotone",
        phonemes=[_phoneme("m", 90.0), _phoneme("ɔ", 85.0), _phoneme("n", 88.0), _phoneme("o", 87.0), _phoneme("t", 90.0), _phoneme("ɔ", 86.0), _phoneme("n", 89.0)],
    )]
    findings = rules.evaluate(words)
    assert not any(f["category"] == "nasalVowel" for f in findings)


# ── French R ──────────────────────────────────────────────────────────────

def test_french_r_flagged_on_low_accuracy():
    words = [_word("rouge", phonemes=[_phoneme("ʁ", 25.0), _phoneme("u", 90.0), _phoneme("ʒ", 88.0)])]
    findings = rules.evaluate(words)
    r_findings = [f for f in findings if f["category"] == "frenchR"]
    assert len(r_findings) == 1
    assert "onset" in r_findings[0]["explanation"] or "start" in r_findings[0]["explanation"]


def test_french_r_coda_position_classified():
    words = [_word("pour", phonemes=[_phoneme("p", 90.0), _phoneme("u", 88.0), _phoneme("ʁ", 15.0)])]
    findings = rules.evaluate(words)
    r_findings = [f for f in findings if f["category"] == "frenchR"]
    assert len(r_findings) == 1
    assert "end" in r_findings[0]["explanation"]


def test_word_with_r_not_flagged_when_accuracy_high():
    # The old heuristic flagged every word containing "r" regardless of
    # actual pronunciation quality; this must not repeat that.
    words = [_word("rouge", phonemes=[_phoneme("ʁ", 95.0), _phoneme("u", 90.0), _phoneme("ʒ", 88.0)])]
    findings = rules.evaluate(words)
    assert not any(f["category"] == "frenchR" for f in findings)


# ── Silent letters ────────────────────────────────────────────────────────

def test_silent_letter_flagged_on_insertion_error():
    words = [_word("petit", error_type="extra")]
    findings = rules.evaluate(words)
    assert any(f["category"] == "silentLetter" and f["word"] == "petit" for f in findings)


def test_silent_letter_not_flagged_without_insertion():
    words = [_word("petit", error_type="correct")]
    findings = rules.evaluate(words)
    assert not any(f["category"] == "silentLetter" for f in findings)


def test_silent_letter_not_flagged_on_non_silent_final_letter():
    words = [_word("chat", error_type="extra")]
    # "t" is a silent-letter-eligible final consonant; a word ending in a
    # vowel should never trigger this rule regardless of errorType.
    words_vowel_ending = [_word("ami", error_type="extra")]
    findings = rules.evaluate(words_vowel_ending)
    assert not any(f["category"] == "silentLetter" for f in findings)


# ── Liaison ───────────────────────────────────────────────────────────────

def test_liaison_flagged_when_gap_exceeds_threshold():
    words = [
        _word("les", offset_ms=1000, duration_ms=150),
        _word("amis", offset_ms=1300, duration_ms=400),  # gap = 1300 - 1150 = 150ms > 80ms max
    ]
    findings = rules.evaluate(words)
    assert any(f["category"] == "liaison" for f in findings)


def test_liaison_not_flagged_when_words_run_together():
    words = [
        _word("les", offset_ms=1000, duration_ms=150),
        _word("amis", offset_ms=1160, duration_ms=400),  # gap = 1160 - 1150 = 10ms, within threshold
    ]
    findings = rules.evaluate(words)
    assert not any(f["category"] == "liaison" for f in findings)


def test_liaison_not_flagged_when_second_word_starts_with_consonant():
    words = [
        _word("les", offset_ms=1000, duration_ms=150),
        _word("chats", offset_ms=1400, duration_ms=400),
    ]
    findings = rules.evaluate(words)
    assert not any(f["category"] == "liaison" for f in findings)


def test_liaison_suppressed_near_chunk_boundary():
    words = [
        _word("les", offset_ms=1000, duration_ms=150, near_boundary=True),
        _word("amis", offset_ms=1300, duration_ms=400),
    ]
    findings = rules.evaluate(words)
    assert not any(f["category"] == "liaison" for f in findings)


def test_liaison_not_flagged_without_offsets():
    # whisper-heuristic tier words carry no offsetMs/durationMs at all.
    words = [_word("les"), _word("amis")]
    findings = rules.evaluate(words)
    assert not any(f["category"] == "liaison" for f in findings)


# ── Findings shape / confidence ceiling ─────────────────────────────────────

def test_all_findings_carry_inferred_provenance_and_ceilinged_confidence():
    words = [_word("vin", phonemes=[_phoneme("v", 92.0), _phoneme("ɛ̃", 20.0)])]
    findings = rules.evaluate(words)
    assert findings
    for f in findings:
        assert f["provenance"] == "inferred"
        assert f["confidence"] <= 0.6


def test_words_with_no_phonemes_produce_no_ipa_anchored_findings():
    # whisper-heuristic tier: no phonemes key at all.
    words = [_word("vin", phonemes=None), _word("rouge", phonemes=None)]
    findings = rules.evaluate(words)
    assert not any(f["category"] in ("nasalVowel", "frenchR") for f in findings)


if __name__ == "__main__":
    import inspect
    names = [n for n, f in list(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for name in names:
        globals()[name]()
    print(f"All {len(names)} test_phonology_fr tests passed.")
