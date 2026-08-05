"""Chunking + aggregation for audio longer than Azure's single-call limits.

Azure's REST short-audio endpoint caps at 30s; this module splits longer
audio into <=25s windows (5s under the cap) on Whisper word boundaries,
assesses each chunk independently, and recombines the results using the
per-metric rules in the accent-analyzer plan §4 — most scores are NOT
simple duration-weighted means; several must be recomputed from the merged
timeline rather than averaged as chunk outputs.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

MAX_CHUNK_SEC = 25.0
SEAM_PROXIMITY_MS = 150
MIN_SUCCESSFUL_DURATION_RATIO = 0.6


def build_chunk_windows(
    whisper_words: list[dict[str, Any]],
    total_duration_sec: float,
    *,
    max_chunk_sec: float = MAX_CHUNK_SEC,
) -> list[tuple[float, float]]:
    """Returns [(startSec, endSec), ...] windows, split on Whisper word
    boundaries so no window ever cuts a word in half. Falls back to a single
    window covering the whole clip when there are no words or the clip
    already fits in one chunk."""
    if total_duration_sec <= max_chunk_sec or not whisper_words:
        return [(0.0, total_duration_sec)]

    windows: list[tuple[float, float]] = []
    window_start = 0.0
    last_word_end = 0.0
    for word in whisper_words:
        word_end = float(word.get("end", last_word_end))
        if word_end - window_start > max_chunk_sec and last_word_end > window_start:
            windows.append((window_start, last_word_end))
            window_start = last_word_end
        last_word_end = word_end
    windows.append((window_start, total_duration_sec))
    return windows


def _duration_weighted_mean(values_with_weights: list[tuple[float, float]]) -> float | None:
    total_weight = sum(w for _, w in values_with_weights if w > 0)
    if total_weight <= 0:
        return None
    return sum(v * w for v, w in values_with_weights if w > 0) / total_weight


def aggregate_chunk_results(
    chunk_results: list[dict[str, Any] | None],
    chunk_windows: list[tuple[float, float]],
) -> dict[str, Any]:
    """Merges per-chunk normalized Azure/whisper-heuristic results (as
    produced by azure_client._normalize_azure_response) into one response.

    `chunk_results[i]` is None for a chunk that failed after retries — its
    window is excluded from every metric and counted in chunksFailed, per
    plan §4's "aggregate what succeeded" rule. A chunk that itself returned
    couldNotAssess is treated the same as a failure for scoring purposes,
    since it contributed no usable assessment.
    """
    successful: list[tuple[dict[str, Any], tuple[float, float]]] = []
    chunks_failed = 0
    total_duration = sum(end - start for start, end in chunk_windows) or 1.0

    for result, window in zip(chunk_results, chunk_windows):
        if result is None or result.get("couldNotAssess"):
            chunks_failed += 1
            continue
        successful.append((result, window))

    successful_duration = sum(end - start for _, (start, end) in successful)
    if not successful or successful_duration / total_duration < MIN_SUCCESSFUL_DURATION_RATIO:
        return {
            "score": None,
            "transcript": " ".join(r.get("transcript", "") for r, _ in successful).strip(),
            "issues": [],
            "words": [],
            "provider": successful[0][0]["provider"] if successful else "whisper-heuristic",
            "subScores": None,
            "couldNotAssess": True,
            "couldNotAssessReason": "assessment_unavailable",
            "chunkCount": len(chunk_windows),
            "chunksFailed": chunks_failed,
        }

    # words[]: concatenate in timeline order, re-index offsets to be
    # relative to the merged clip (Azure offsets are chunk-relative).
    merged_words: list[dict[str, Any]] = []
    for result, (chunk_start_sec, chunk_end_sec) in successful:
        chunk_start_ms = int(chunk_start_sec * 1000)
        chunk_end_ms = int(chunk_end_sec * 1000)
        for word in result.get("words", []):
            offset_ms = word.get("offsetMs")
            new_offset_ms = (offset_ms + chunk_start_ms) if offset_ms is not None else None
            near_boundary = False
            if new_offset_ms is not None:
                near_boundary = (
                    new_offset_ms - chunk_start_ms < SEAM_PROXIMITY_MS
                    or chunk_end_ms - new_offset_ms < SEAM_PROXIMITY_MS
                )
            merged_words.append({
                **word,
                "offsetMs": new_offset_ms,
                "nearChunkBoundary": near_boundary,
            })

    merged_issues: list[dict[str, Any]] = []
    for result, _ in successful:
        merged_issues.extend(result.get("issues", []))

    # accuracy: duration-weighted mean over merged words, not mean-of-chunks
    # (a mean of chunk means over-weights short chunks).
    accuracy_pairs = [
        (r["subScores"]["accuracy"], end - start)
        for r, (start, end) in successful
        if r.get("subScores")
    ]
    accuracy = _duration_weighted_mean(accuracy_pairs)

    # completeness: recompute globally as matched/total reference words —
    # averaging per-chunk percentages is wrong (2/2 and 1/10 average to 60%,
    # truth is 25%). We don't have raw ref/matched counts here, so approximate
    # via the correct-word ratio across merged_words as the best available
    # proxy; a chunk lacking completeness (freeform/whisper-heuristic)
    # excludes itself rather than being treated as 0%.
    completeness_words = [w for w in merged_words if w.get("errorType") is not None]
    completeness = None
    if completeness_words:
        correct = sum(1 for w in completeness_words if w["errorType"] != "skipped")
        completeness = round(100 * correct / len(completeness_words), 1)

    fluency_pairs = [
        (r["subScores"]["fluency"], end - start)
        for r, (start, end) in successful
        if r.get("subScores")
    ]
    fluency = _duration_weighted_mean(fluency_pairs)

    pron_score_pairs = [
        (r["score"], end - start) for r, (start, end) in successful if r.get("score") is not None
    ]
    pron_score = _duration_weighted_mean(pron_score_pairs)

    sub_scores = None
    if accuracy is not None or fluency is not None:
        sub_scores = {
            "accuracy": accuracy if accuracy is not None else 0.0,
            "fluency": fluency if fluency is not None else 0.0,
            "completeness": completeness,
            "prosody": None,
        }

    provider = successful[0][0]["provider"]
    transcript = " ".join(r.get("transcript", "") for r, _ in successful).strip()

    return {
        "score": round(pron_score) if pron_score is not None else None,
        "transcript": transcript,
        "issues": merged_issues,
        "words": merged_words,
        "provider": provider,
        "subScores": sub_scores,
        "couldNotAssess": False,
        "couldNotAssessReason": None,
        "chunkCount": len(chunk_windows),
        "chunksFailed": chunks_failed,
    }


async def assess_chunked(
    audio_windows: list[bytes],
    chunk_windows: list[tuple[float, float]],
    assess_one_chunk: Callable[[bytes], Awaitable[dict[str, Any] | None]],
    *,
    max_concurrent: int = 3,
) -> dict[str, Any]:
    """Fans out `assess_one_chunk` over each audio window bounded by a
    semaphore (plan §9: fan out with asyncio.gather bounded by a semaphore of
    3), then merges via aggregate_chunk_results. A chunk whose assessment
    raises is treated as a failure (None), not a request-ending exception —
    plan §4's "fail the chunk, not the request." """
    import asyncio

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _bounded(audio_bytes: bytes) -> dict[str, Any] | None:
        async with semaphore:
            try:
                return await assess_one_chunk(audio_bytes)
            except Exception:
                return None

    results = await asyncio.gather(*(_bounded(chunk) for chunk in audio_windows))
    return aggregate_chunk_results(list(results), chunk_windows)
