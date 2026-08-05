"""Derived prosody (accent-analyzer plan §7). Azure supplies no ProsodyScore,
syllable groups, or rhythm metrics for fr-FR — en-US only (plan §5, §7). This
module computes rhythm metrics ourselves from the merged word timeline
(`offsetMs`/`durationMs`), which Azure DOES supply for every word regardless
of locale.

Everything here is provenance="derived", never "authoritative" — and the
capability matrix (fr.json's rhythmMetrics: "derived") is what actually
gates whether this gets attached to a response at all for a given
(mode, tier); the whisper-heuristic tier has no word timings (Groq returns
no words — plan's "two more constraints" section) so this module is never
called for that tier's results.

Publishes no aggregate "prosody score" (plan §7) — only labelled components.
"""

from __future__ import annotations

from typing import Any

# A pause between words this long or longer counts toward pause statistics;
# below this, it's ordinary inter-word timing, not a hesitation or
# grammatical pause. Matches the "pauses >= 250ms" articulation-rate
# exclusion the plan specifies for pause counting/artic rate.
PAUSE_THRESHOLD_MS = 250

_VOWEL_LETTERS = set("aeiouyàâäéèêëîïôöùûü")


def _estimate_syllable_count(word: str) -> int:
    """Crude vowel-group count as a syllable proxy — Azure gives no syllable
    boundaries for French. Every word has at least one syllable."""
    count = 0
    prev_was_vowel = False
    for ch in word.lower():
        is_vowel = ch in _VOWEL_LETTERS
        if is_vowel and not prev_was_vowel:
            count += 1
        prev_was_vowel = is_vowel
    return max(1, count)


def compute_rhythm_metrics(words: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Computes PronunciationRhythmMetrics from a merged word timeline.
    Returns None when there isn't enough timing data to compute anything
    (e.g. fewer than 2 timed words) — never a fabricated set of zeros."""
    timed_words = [
        w for w in words
        if w.get("offsetMs") is not None and w.get("durationMs") is not None
    ]
    if len(timed_words) < 2:
        return None

    timed_words = sorted(timed_words, key=lambda w: w["offsetMs"])

    first_offset = timed_words[0]["offsetMs"]
    last_word = timed_words[-1]
    total_span_ms = (last_word["offsetMs"] + last_word["durationMs"]) - first_offset
    if total_span_ms <= 0:
        return None

    # Speech rate: words per minute over the full span (pauses included —
    # this measures overall pacing, not just articulation).
    speech_rate_wpm = round(len(timed_words) / (total_span_ms / 60000), 1)

    # Pause profile: gaps between consecutive words' end and next start.
    pause_gaps_ms: list[int] = []
    for i in range(len(timed_words) - 1):
        current = timed_words[i]
        nxt = timed_words[i + 1]
        gap = nxt["offsetMs"] - (current["offsetMs"] + current["durationMs"])
        if gap >= PAUSE_THRESHOLD_MS:
            pause_gaps_ms.append(gap)

    pause_count = len(pause_gaps_ms)
    longest_pause_ms = max(pause_gaps_ms) if pause_gaps_ms else 0
    total_pause_ms = sum(pause_gaps_ms)
    pause_ratio = round(total_pause_ms / total_span_ms, 3) if total_span_ms > 0 else None

    # Articulation rate: syllables per second, EXCLUDING pauses >= 250ms —
    # measures speed of actual articulation, distinct from speech rate which
    # includes pause time.
    total_syllables = sum(_estimate_syllable_count(w["word"]) for w in timed_words)
    articulation_time_ms = total_span_ms - total_pause_ms
    articulation_rate = (
        round(total_syllables / (articulation_time_ms / 1000), 2)
        if articulation_time_ms > 0
        else None
    )

    # Rhythm regularity: normalised pairwise variability index (nPVI) of
    # syllable durations. French is syllable-timed (low nPVI expected);
    # anglophones import stress-timed rhythm (high nPVI) — a genuinely
    # diagnostic signal per plan §7. Approximate per-word duration as a
    # proxy for per-syllable duration divided across estimated syllables.
    syllable_durations_ms = [
        w["durationMs"] / _estimate_syllable_count(w["word"]) for w in timed_words
    ]
    rhythm_regularity = _normalized_pairwise_variability(syllable_durations_ms)

    # Final-syllable lengthening: French lengthens the last syllable of a
    # rhythmic group. Compare the last word's per-syllable duration against
    # the mean of the preceding words' — a ratio > 1.2 suggests lengthening
    # was present; absence is a non-native marker per plan §7.
    final_syllable_lengthening = _detect_final_lengthening(syllable_durations_ms)

    return {
        "speechRateWpm": speech_rate_wpm,
        "articulationRateSyllPerSec": articulation_rate,
        "pauseCount": pause_count,
        "longestPauseMs": longest_pause_ms,
        "pauseRatio": pause_ratio,
        "rhythmRegularity": rhythm_regularity,
        "finalSyllableLengthening": final_syllable_lengthening,
    }


def _normalized_pairwise_variability(durations_ms: list[float]) -> float | None:
    if len(durations_ms) < 2:
        return None
    terms = []
    for i in range(len(durations_ms) - 1):
        d1, d2 = durations_ms[i], durations_ms[i + 1]
        denom = (d1 + d2) / 2
        if denom <= 0:
            continue
        terms.append(abs(d1 - d2) / denom)
    if not terms:
        return None
    return round(100 * sum(terms) / len(terms), 1)


def _detect_final_lengthening(syllable_durations_ms: list[float]) -> bool | None:
    if len(syllable_durations_ms) < 2:
        return None
    *preceding, final = syllable_durations_ms
    mean_preceding = sum(preceding) / len(preceding)
    if mean_preceding <= 0:
        return None
    return final / mean_preceding > 1.2
