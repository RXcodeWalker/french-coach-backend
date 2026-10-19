# Learn marking gate report — groq

- Observed calls: 25 (22 gate-stage, 3 smoke)
- Run 1 scored: 21 of 22 answers
- answers.json commit(s): 3a1b7e9fc10bd0e2c7736cf821a61a69bfb58247
- Model(s): groq/openai/gpt-oss-120b
- Versions: {"demands": "3efb9504acfd5f91e03df10f47e1ce058cd0b89982986e809bd10bf928ec1c75", "ladder": "learn-ladder-v1-139f19a106d2", "learnPrompt": "learn-prompt-v8", "systemPromptSha256": "8805d5a5814b1bfcd912cb1e9c2f33cbb47be9be0933ab03fa0fd5f338e646a0"}

| # | Gate | Threshold | Result | Detail |
|---|---|---|---|---|
| G1 | Order A1 ≥ A2 ≥ Exam ≥ B1 ≥ B2 | 100% of answer-runs | **PASS** | 21/21 |
| G2 | Big gap for simple answers | A1 ≥ 8.0, A1−B1 ≥ 3.0, A1−B2 ≥ 4.5 | **PASS** | 5/5 scored, none outside the threshold |
| G3 | Top of scale used | strong B1: A2 ≥ 9.0, B1 ≥ 7.5 | **PASS** | 5/5 scored, none outside the threshold |
| G4 | Typical A2 sits at A2 | A2 ≥ 7.5, B1 ≤ A2 − 2.0 | **FAIL** | hol_01_typical_a2 A2 7.1 B1 5.3; env_05_typical_a2 A2 9.5 B1 7.8; fut_19_typical_a2 A2 9.3 B1 7.5 |
| G5 | Exam really in between | A2−Exam ≥ 0.5 for ≥ 4/5 typical; Exam−B1 ≥ 0.5 for ≥ 4/5 strong | **PASS** | typical 5/5, strong 5/5 |
| G6 | Mistakes count at Exam | weak Exam ≤ 6.5; recall ≥ 80%; no slip-only Accuracy failure | **PASS** | recall 18/20 (90%) |
| G7 | Off-topic | ≤ 2.0 at every level | **PASS** | all ≤ 2.0 |
| G8 | Verdicts match expectations | ≥ 85% all; ≥ 90% deciding; every ladder ≥ 75% | **PASS** | unscored, counted as all wrong: fut_19_b2; all 365/409 (89%), deciding 179/195 (92%), A 90%, F 85%, L 93%, O 83%, R 91%, T 86% |
| G9 | Stability (3 runs) | flip ≤ 10%; spread ≤ 1.0 each; mean ≤ 0.5 | **FAIL** | incomplete — run Stage 3; flips 0/0 (0%), mean spread 0.00 |
| G10 | Evidence is real | ≥ 95% of "y" quotes in the transcript | **PASS** | 99/102 (97%) |
| G11 | Contract and cost | ≥ 97.5% valid; demandCheck 100%; 0 truncations; median ≤ 6,000, max ≤ 8,000 | **FAIL** | valid 24/25 (96.0%), demandCheck 24/24, truncated 4 (fam_01_weak#1, hol_01_weak#1, env_05_weak#1, fut_19_b2#1), tokens median 6412 max 7378 (25 with usage) |
| G12 | Needs list | simple A1: 0 at A1, ≥ 2 at B1; never a non-applicable ladder | **FAIL** | fam_01_simple_a1: A1 1, B1 3 |
| G13 | Owner read-through | yes for ≥ 4/5 simple-A1 'needs for B1' lists | **OWNER** | see 'For the owner' below |
| G14 | Only what the question asks | no stray needs item (unit-proven); hol_01 strong B1 ≥ B1 everywhere | **PASS** | hol_01_strong_b1: B1+ on every ladder |

**Automatic gates: NOT PASSED** (G13 is the owner's).

Run 1 missing for: fut_19_b2

## Scores, run 1 (overall per level)

| Answer | A1 | A2 | Exam | B1 | B2 | Reached |
|---|---|---|---|---|---|---|
| fam_01_simple_a1 | 8.8 | 7.1 | 6.8 | 5.3 | 4.0 | T:A2 L:none R:A2 A:B2 |
| fam_01_typical_a2 | 10.0 | 9.0 | 8.3 | 7.0 | 5.0 | T:B1 L:A2 R:A2 A:B2 |
| fam_01_strong_b1 | 10.0 | 10.0 | 9.5 | 8.5 | 6.3 | T:B1 L:B1 R:B1 A:B2 |
| fam_01_weak | 9.5 | 7.3 | 5.9 | 4.4 | 2.1 | T:A2 L:A1 R:A2 A:A2 |
| hol_01_simple_a1 | 9.5 | 7.8 | 6.8 | 5.6 | 4.0 | T:A2 L:A1 F:A2 R:A2 A:B2 |
| hol_01_typical_a2 | 8.8 | 7.1 | 6.0 | 5.3 | 4.0 | T:A2 L:A2 F:A2 R:none A:B2 |
| hol_01_strong_b1 | 10.0 | 10.0 | 9.3 | 8.5 | 6.3 | T:B1 L:B1 F:B1 R:B1 A:B2 |
| hol_01_weak | 9.5 | 7.3 | 5.5 | 4.4 | 2.1 | T:A2 L:A1 F:A2 R:A2 A:A2 |
| sch_03_simple_a1 | 9.5 | 8.3 | 7.6 | 6.4 | 4.6 | T:B1 L:A1 R:A2 A:B2 |
| sch_03_typical_a2 | 10.0 | 9.0 | 8.3 | 7.0 | 5.0 | T:B1 L:A2 R:A2 A:B2 |
| sch_03_strong_b1 | 10.0 | 10.0 | 9.5 | 9.0 | 7.5 | T:B2 L:B1 R:B1 A:B2 |
| sch_03_weak | 9.5 | 7.8 | 6.4 | 5.1 | 2.8 | T:B1 L:A1 R:A2 A:A2 |
| env_05_simple_a1 | 9.5 | 7.8 | 6.8 | 5.6 | 4.0 | T:A2 L:A1 O:A2 R:A2 A:B2 |
| env_05_typical_a2 | 10.0 | 9.5 | 8.5 | 7.8 | 5.6 | T:B1 L:A2 O:B1 R:B1 A:B2 |
| env_05_strong_b1 | 10.0 | 10.0 | 9.8 | 9.0 | 7.5 | T:B1 L:B2 O:B1 R:B1 A:B2 |
| env_05_weak | 9.5 | 7.3 | 5.5 | 4.4 | 2.1 | T:A2 L:A1 O:A2 R:A2 A:A2 |
| env_05_off_topic | 2.0 | 2.0 | 1.8 | 1.7 | 1.5 | T:none L:none O:A1 R:B1 A:B2 |
| fut_19_simple_a1 | 9.5 | 8.3 | 7.3 | 6.4 | 4.6 | T:B1 L:A1 O:B1 F:B1 R:A2 A:B2 |
| fut_19_typical_a2 | 10.0 | 9.3 | 8.2 | 7.5 | 5.4 | T:B1 L:A2 O:B1 F:A2 R:B1 A:B2 |
| fut_19_strong_b1 | 10.0 | 9.5 | 8.7 | 7.8 | 5.6 | T:B1 L:B1 O:B1 F:B1 R:A2 A:B2 |
| fut_19_weak | 8.8 | 6.4 | 4.7 | 3.8 | 1.8 | T:A2 L:A2 O:B1 F:A1 R:A1 A:A1 |
| fut_19_b2 | — | — | — | — | — | not scored |

## For the owner (G13): what each simple answer needs for B1

**fam_01_simple_a1** — « Dans ma famille, il y a quatre personnes : mon père, ma mère, mon frère et moi. Mon père est grand. Ma mère est très gentille. Mon frère a dix ans. »
- Developing your answer: Put your points in a sequence and develop one of them with an extra detail or an example. — e.g. « Mon père est grand et il travaille comme ingénieur. »
- Linking your ideas (first step): Link your ideas with simple words like et or puis. — e.g. « Mon père est grand et travaille dans la construction. »
- Range of language: Add some sentences with a second clause (qui, que, quand, si, parce que) and use a wider range of topic words. — e.g. « Mon frère a dix ans et il adore le football. »

**hol_01_simple_a1** — « Pendant les dernières vacances, je suis allé en Espagne avec mes parents. Nous avons pris l'avion. J'ai nagé dans la mer et j'ai mangé des glaces. Mon frère a joué au tennis. »
- Developing your answer: Put your points in a sequence and develop one of them with an extra detail or an example. — e.g. « J'ai nagé dans la mer, ce qui était rafraîchissant »
- Talking about the past: Say how you felt or reacted to what happened. — e.g. « J'ai nagé dans la mer et j'étais très heureux »
- Linking your ideas (first step): Use simple connectors like mais, parce que or car. — e.g. « J'ai nagé dans la mer, mais il faisait froid »

**sch_03_simple_a1** — « Je vais à l'école en bus. Le bus passe à sept heures trente. Le trajet dure vingt minutes. Je prends le bus avec mon amie Léa et nous parlons beaucoup. »
- Linking your ideas (first step): Use simple connectors like mais, parce que or car. — e.g. « Je prends le bus, mais parfois il y a du retard. »
- Range of language: Add some sentences with a second clause (qui, que, quand, si, parce que) and use a wider range of topic words. — e.g. « Quand le bus arrive, je monte rapidement. »

**env_05_simple_a1** — « Le recyclage, c'est important. À la maison, je recycle le papier et les bouteilles. Ma mère recycle le verre. Au collège, il y a des poubelles jaunes. J'aime le recyclage. »
- Developing your answer: Put your points in a sequence and develop one of them with an extra detail or an example. — e.g. « J'aime le recyclage parce que ça réduit les déchets. »
- Opinions and reasons: Give at least one reason for your opinion. — e.g. « J'aime le recyclage parce que c'est bon pour la planète. »
- Linking your ideas (first step): Use simple connectors like mais, parce que or car. — e.g. « et parce que ça protège l'environnement. »

**fut_19_simple_a1** — « Oui, j'aimerais être célèbre. Je voudrais être footballeur. Je joue au foot tous les samedis et j'adore ça. Mon joueur préféré est Mbappé. Il joue pour le Real Madrid. »
- Linking your ideas (first step): Use simple connectors like mais, parce que or car. — e.g. « Je veux être célèbre parce que j'aime le foot, mais je crains la perte de confidentialité. »
- Range of language: Add some sentences with a second clause (qui, que, quand, si, parce que) and use a wider range of topic words. — e.g. « Je veux être footballeur parce que j'admire les joueurs comme Mbappé. »

