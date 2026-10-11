# Exam-pronunciation calibration — failed run (fairness v1, 2026-10-11)

Recorded Azure/Whisper responses that **failed** the Batch 7 release gate. Kept as evidence; the CI gate
(`tests/fixtures/exam_pronunciation_calibration/`) deliberately does not read this folder, so CI stays
green while the failure stays on record. Full write-up: the main repo's `verification-log.md`
(2026-10-11) and `docs/systems/exam-pronunciation.md`.

- `accented/` — 20 learner-accent clips, Common Voice 27.0 French (CC0; UK, US, German, Romanian,
  Dutch, anglophone-Canadian speakers). One false positive: `existe` (accuracy 28, uncategorised,
  both recognisers agreed — understandable, accented; confirmed by the owner listening).
- `unclear/` — set 3, the owner's own recordings (consenting, 13+): 9 sentences read correctly and
  9 with one deliberate minimal-pair swap. Correct readings: 0 reported. Swaps: **0 of 9 caught** —
  freeform assessment grades the audio against Whisper's transcript of the same audio, so a cleanly
  substituted word is transcribed and scored as itself.

Replay these with the same tooling (`services/pronunciation/calibration_replay.py`; the main repo's
`npm run pronunciation:calibration:report -- --dir backend/tests/fixtures/exam_pronunciation_calibration_failed_v1`).
