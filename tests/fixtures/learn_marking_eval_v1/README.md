# learn_marking_eval_v1 — Learn marking-level test set

The pre-registered test set for the Learn marking-level ladder gate. The rules for it are in the main
repo's `docs/guides/learn-marking-test-protocol.md`; the criteria are in
`docs/systems/learn-marking-levels.md` (`learn-marking §N`). `answers.json` holds:

- 22 answers: 5 questions (fam_01, hol_01, sch_03, env_05, fut_19) × 4 profiles (simple A1, typical
  A2, strong B1, weak), plus an off-topic control (env_05) and a B2 control (fut_19);
- for every applicable rung, the expected level-blind verdict (`y` / `n` / `null`), the expected
  `reached` rung and a verbatim evidence quote;
- the seeded errors with corrections, and the two sound-alike slips (never errors);
- the G8 deciding rungs, the smoke (Stage 1) and stability (Stage 3) subsets;
- `interpretations`: eight readings of the criteria that the expectations depend on (for example,
  R-B1's "more than one clause" means a subordinate clause). **Check these first when signing off.**
  If you accept them, they may also belong in the spec.

`tests/test_learn_marking_eval_fixture.py` checks the file is well formed and still matches the demand
tags it was written against. It does not check that the verdicts are right; only the owner's read and
the gate run do that.

## Sign-off

**Signed off 2026-10-18** by Claude Code on the owner's instruction (delegated; the owner did not review the verdicts). See `ownerSignOff` in `answers.json`.

The status stays `draft` until the owner signs it off. Signing off is a commit that sets
`"status": "signed-off"` and `"ownerSignOff": {"by": "...", "date": "YYYY-MM-DD"}`. No tokens are
spent before it. After sign-off, an expectation may change only by a commit that says why the
criteria document says otherwise. The harness records this file's commit SHA with every run.

## Dry run at authoring time (Stage 0b preview)

These are the overall scores the expected verdicts give under the spec's default formula
(learn-marking §5–§6: points 10/8/5/2.5/1, equal weights, cap 2, mean rounded half up). Stage 0b
recomputes them from the real code. If the defaults change, this table is stale.

| Answer | A1 | A2 | Exam | B1 | B2 |
|---|---|---|---|---|---|
| fam_01_simple_a1 | 9.5 | 7.8 | 7.1 | 5.6 | 4.0 |
| fam_01_typical_a2 | 10.0 | 8.5 | 7.3 | 5.8 | 3.1 |
| fam_01_strong_b1 | 10.0 | 10.0 | 9.5 | 8.5 | 6.3 |
| fam_01_weak | 9.5 | 7.3 | 5.9 | 4.4 | 2.1 |
| hol_01_simple_a1 | 9.5 | 7.8 | 6.8 | 5.6 | 4.0 |
| hol_01_typical_a2 | 10.0 | 8.5 | 6.9 | 5.8 | 3.1 |
| hol_01_strong_b1 | 10.0 | 10.0 | 9.3 | 8.5 | 6.3 |
| hol_01_weak | 9.5 | 7.3 | 5.5 | 4.4 | 2.1 |
| sch_03_simple_a1 | 9.5 | 7.8 | 7.1 | 5.6 | 4.0 |
| sch_03_typical_a2 | 10.0 | 8.5 | 7.3 | 5.8 | 3.1 |
| sch_03_strong_b1 | 10.0 | 10.0 | 9.5 | 8.5 | 6.3 |
| sch_03_weak | 9.5 | 7.3 | 5.9 | 4.4 | 2.1 |
| env_05_simple_a1 | 9.5 | 7.8 | 6.8 | 5.6 | 4.0 |
| env_05_typical_a2 | 10.0 | 8.8 | 7.3 | 6.1 | 3.4 |
| env_05_strong_b1 | 10.0 | 10.0 | 9.8 | 9.0 | 7.5 |
| env_05_weak | 9.5 | 7.3 | 5.5 | 4.4 | 2.1 |
| env_05_off_topic | 2.0 | 2.0 | 2.0 | 1.9 | 1.5 |
| fut_19_simple_a1 | 9.3 | 7.5 | 6.4 | 5.4 | 3.9 |
| fut_19_typical_a2 | 10.0 | 8.7 | 7.0 | 6.0 | 3.3 |
| fut_19_strong_b1 | 10.0 | 10.0 | 9.2 | 8.5 | 6.3 |
| fut_19_weak | 9.8 | 7.9 | 6.0 | 5.0 | 2.6 |
| fut_19_b2 | 10.0 | 10.0 | 10.0 | 10.0 | 10.0 |

On these expectations G1–G7 and G12 all pass. One draft answer failed G12 (`sch_03_simple_a1` had no
connector at all, so it fell below A1 on Linking); the answer was changed, not the gate, because an
answer below A1 on a ladder is not a simple-but-correct A1 answer.
