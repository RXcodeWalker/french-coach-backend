# Phase 0 probe findings (2026-08-04)

Live call against `AZURE_SPEECH_REGION=centralindia`, `fr-FR`, synthesized
via Azure TTS (`fr-FR-DeniseNeural`) so the reference text and spoken audio
are guaranteed to match. Raw responses committed alongside this file:
`response_as_is.json`, `response_corrected_legacy_host.json`.

## §18 unknowns — resolved

| Unknown | Answer | Evidence |
|---|---|---|
| Does `NBestPhonemeCount` work in the REST header JSON? | **Yes.** Sending `NBestPhonemeCount: 5` in the `Pronunciation-Assessment` header produces `Words[].Phonemes[].NBestPhonemes[]` (5 entries per phoneme, each `{Phoneme, Score}`), absent when the param is omitted. | `response_as_is.json` (no param → no `NBestPhonemes` key) vs `response_corrected_legacy_host.json` (param set → present) |
| Does the legacy regional endpoint still work? | **Yes.** `https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1` returned 200 for both calls. Resource-scoped host was never attempted (not needed). | Both response files, `status_code: 200` |
| Which response shape does this resource/region return? | **Flat**, confirming defect #7 exactly as documented. `NBest[0]` has `AccuracyScore`/`FluencyScore`/`CompletenessScore`/`PronScore` directly on it — no `PronunciationAssessment` wrapper. Same for `Words[]`: `AccuracyScore`/`ErrorType` directly on the word object. `RecognitionStatus` is the **string** `"Success"`, not integer `0`. | Programmatic check: `'PronunciationAssessment' in NBest[0]` → `False`; `'PronunciationAssessment' in Words[0]` → `False`; `type(RecognitionStatus)` → `str` |

## Defects confirmed live

- **#1 (Content-Type)**: sending `audio/webm; codecs=opus` against real 16kHz mono PCM WAV bytes still returned `200` with a full, non-degraded result (`AccuracyScore: 100.0` etc.) in this probe — Azure did not reject it outright here. **This narrows, not clears, defect #1**: the REST docs still state only two accepted Content-Types, and real browser output is actual webm/opus-encoded audio (not WAV bytes mislabeled as webm, which is what this probe sent). The mislabeling in this probe is favorable-case (correctly-formatted PCM, wrong label) and is not evidence that genuine webm/opus audio is handled — only that Azure isn't strictly validating the label against payload bytes in all cases. Do not treat this as clearing defect #1; the fix (correct Content-Type matching actual encoded payload) proceeds as planned.
- **#7 (JSON shape)**: **confirmed exactly as documented.** Both calls returned the flat REST shape. `azure_client.py`'s `best.get("PronunciationAssessment") or {}` resolves to `{}` against this real response — every sub-score would read as `0.0` and every word `errorType` as `"correct"` (default), exactly as the plan predicted. This is the highest-priority fix.
- **`RecognitionStatus` is a string** (`"Success"`), confirming defect #4's premise — the SDK-shape assumption of an integer `0` would also be wrong even if the nested-shape bug were the only issue.

## Unexpected finding not in the original plan

**`Phoneme` field is an empty string (`""`) for every phoneme in every word**, in both calls (visible in raw JSON, not a parsing artifact — checked programmatically). Same for `Syllable` field on several syllables (some have `Grapheme` populated, `Syllable` empty). This means:

- `ipaExpected`/`ipaHeard` derivation (defect #3 fix) will produce sequences of **empty strings joined by spaces**, not actual IPA symbols, for this resource/region/voice combination.
- This is very likely a **region/voice support gap**: `centralindia` may not have full phoneme-label support for `fr-FR`, or `PhonemeAlphabet: IPA` may need a different parameter combination, or this specific neural voice's synthesized audio doesn't get phoneme labels back (as opposed to real human speech). `AccuracyScore` per-phoneme is populated correctly (100.0, 97.0, etc.) — only the `Phoneme` label itself is blank.
- **This was not one of the plan's three named unknowns and is not resolved by this probe alone.** It affects §6 (French phonology rules, which key off "the expected IPA sequence Azure returns") and the IPA-related parts of §3's capability matrix (`observedIpa`/phoneme-level fields).

## Recommendation

Unknowns 1–3 are resolved cleanly and Phase 0 steps 2–5 (frontend passthrough release, backend fixes #1/#2/#4/#6/#7/#8, normalizer, Learn.tsx reference-text fix) can proceed as planned — none of them depend on phoneme labels being non-empty.

The empty-`Phoneme` finding is new information the plan didn't anticipate. It specifically threatens §6 (phonology rules) and the `observedIpa`/phoneme capability-matrix entries in §3, both **Phase 2** work, not Phase 0. Recommend re-probing with real human-recorded audio (not synthesized) before Phase 2 begins, to check whether this is a synthesized-speech artifact or a genuine resource/region gap — flagging now per the instruction to stop and surface plan/reality contradictions rather than silently proceeding, since Phase 2 as written assumes non-empty IPA phoneme labels are available.
