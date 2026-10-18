"""Integrity of the pre-registered Learn marking-level test set
(tests/fixtures/learn_marking_eval_v1/answers.json; frontend docs/guides/learn-marking-test-protocol.md).

The expected verdicts are hand-written from the criteria document (learn-marking §3-§6). These
tests check only that the file is well formed and still matches the corpus it was written
against: every quote is verbatim, each `reached` follows from its verdicts, the profiles hold,
and the ladders that apply still follow from the live demand tags (learn-marking §4). A re-tag
of one of the five questions fails here, because the expectations would then be stale.

Run: pytest tests/test_learn_marking_eval_fixture.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # conftest.py imports lib.consent
FIXTURE = ROOT / "tests" / "fixtures" / "learn_marking_eval_v1" / "answers.json"
DEMANDS_DIR = ROOT / "data" / "learn"

DOC = json.loads(FIXTURE.read_text(encoding="utf-8"))
ANSWERS = DOC["answers"]
LEVELS = ["A1", "A2", "B1", "B2"]
QUESTION_IDS = ["fam_01", "hol_01", "sch_03", "env_05", "fut_19"]
MAIN_TYPES = ["simple_a1", "typical_a2", "strong_b1", "weak"]


def _live_demands(question_id: str) -> dict:
    for path in DEMANDS_DIR.glob("*.json"):
        for entry in json.loads(path.read_text(encoding="utf-8")).get("entries", []):
            if entry.get("questionId") == question_id:
                return entry["demands"]
    raise AssertionError(f"{question_id} not in data/learn")


def _applies(demands: dict) -> list[str]:
    """learn-marking §4, written out independently of the scorer."""
    structures = set(demands.get("structures") or [])
    time_frames = set(demands.get("timeFrames") or [])
    ladders = ["T", "L"]
    if structures & {"opinion", "justification", "comparison"} or demands.get("cognitiveDemand") in {
        "justify", "compare", "hypothesize"
    }:
        ladders.append("O")
    if time_frames & {"past", "future", "conditional"}:
        ladders.append("F")
    return ladders + ["R", "A"]


def _reached(verdicts: list) -> str:
    rung = 0
    for verdict in verdicts:
        if verdict == "n":
            break
        rung += 1
    return "none" if rung == 0 else LEVELS[rung - 1]


def _words(text: str) -> int:
    return len([token for token in text.split() if re.search(r"\w", token)])


def test_composition_is_the_pre_registered_22():
    assert len(ANSWERS) == 22
    ids = [a["id"] for a in ANSWERS]
    assert len(set(ids)) == 22
    for q in QUESTION_IDS:
        assert sorted(a["type"] for a in ANSWERS if a["questionId"] == q and a["type"] in MAIN_TYPES) == sorted(MAIN_TYPES)
    controls = {(a["type"], a["questionId"]) for a in ANSWERS if a["type"].startswith("control_")}
    assert controls == {("control_off_topic", "env_05"), ("control_b2", "fut_19")}


def test_question_demands_still_match_the_corpus():
    for q in QUESTION_IDS:
        live = _live_demands(q)
        snapshot = DOC["questions"][q]["demands"]
        for key in ("cognitiveDemand", "timeFrames", "structures"):
            assert live[key] == snapshot[key], f"{q}.{key} changed: re-check the expected verdicts"
        assert DOC["questions"][q]["applies"] == _applies(live)


def test_applies_matches_the_spec_table():
    assert {q: DOC["questions"][q]["applies"] for q in QUESTION_IDS} == {
        "fam_01": ["T", "L", "R", "A"],
        "hol_01": ["T", "L", "F", "R", "A"],
        "sch_03": ["T", "L", "R", "A"],
        "env_05": ["T", "L", "O", "R", "A"],
        "fut_19": ["T", "L", "O", "F", "R", "A"],
    }


@pytest.mark.parametrize("answer", ANSWERS, ids=lambda a: a["id"])
def test_ladders_and_verdicts_are_well_formed(answer):
    assert list(answer["ladders"]) == DOC["questions"][answer["questionId"]]["applies"]
    for name, ladder in answer["ladders"].items():
        verdicts = ladder["verdicts"]
        assert len(verdicts) == 4
        assert all(v in ("y", "n", None) for v in verdicts)
        if name in ("O", "F"):
            assert verdicts[0] is None, "O and F have no A1 rung"
            assert None not in verdicts[1:]
        else:
            assert None not in verdicts
        assert ladder["reached"] == _reached(verdicts)
        # A "yes" above a "no" is ignored by the scorer; an expectation never writes one.
        assert "y" not in verdicts[verdicts.index("n"):] if "n" in verdicts else True
        if "y" in verdicts:
            assert ladder["evidence"] in answer["transcript"], f"{name} evidence is not verbatim"
    assert answer["answeredTheQuestion"] == (answer["ladders"]["T"]["verdicts"][0] == "y")


@pytest.mark.parametrize("answer", ANSWERS, ids=lambda a: a["id"])
def test_profile_word_count_and_errors(answer):
    assert answer["wordCount"] == _words(answer["transcript"])
    profile = DOC["profiles"][answer["type"]]
    if "words" in profile:
        low, high = profile["words"]
        assert low <= answer["wordCount"] <= high
        low, high = profile["seededErrors"]
        assert low <= len(answer["seededErrors"]) <= high
    for error in answer["seededErrors"]:
        assert error["quote"] in answer["transcript"]
        assert error["correction"] != error["quote"]
    for slip in answer["soundAlikeSlips"]:
        assert slip["quote"] in answer["transcript"]
        assert not any(slip["quote"] in e["quote"] or e["quote"] in slip["quote"] for e in answer["seededErrors"])


def test_two_weak_answers_carry_one_sound_alike_slip():
    slips = {a["id"]: len(a["soundAlikeSlips"]) for a in ANSWERS if a["soundAlikeSlips"]}
    assert len(slips) == 2 and set(slips.values()) == {1}
    assert all(next(a for a in ANSWERS if a["id"] == i)["type"] == "weak" for i in slips)


def test_controls_and_deciding_rungs():
    off_topic = next(a for a in ANSWERS if a["type"] == "control_off_topic")
    assert off_topic["ladders"]["T"]["verdicts"][0] == "n"
    assert off_topic["decidingRungs"] == [["T", "A1"]]
    b2 = next(a for a in ANSWERS if a["type"] == "control_b2")
    assert all(l["reached"] == "B2" for l in b2["ladders"].values())
    for answer in ANSWERS:
        if answer["writtenLevel"] is None:
            continue
        i = LEVELS.index(answer["writtenLevel"])
        wanted = [
            [name, LEVELS[j]]
            for name, ladder in answer["ladders"].items()
            for j in [i, i + 1] if j < 4 and ladder["verdicts"][j] is not None
        ]
        assert answer["decidingRungs"] == wanted, answer["id"]


def test_g14_answer_reaches_b1_everywhere_with_the_past_only():
    hol = next(a for a in ANSWERS if a["id"] == "hol_01_strong_b1")
    assert DOC["questions"]["hol_01"]["demands"]["timeFrames"] == ["past"]
    assert all(LEVELS.index(l["reached"]) >= LEVELS.index("B1") for l in hol["ladders"].values())


def test_stage_subsets_are_pre_registered():
    ids = {a["id"]: a for a in ANSWERS}
    assert DOC["stages"]["smoke"]["answers"] == ["fam_01_simple_a1", "fam_01_strong_b1", "fam_01_weak"]
    stability = DOC["stages"]["stability"]["answers"]
    assert len(stability) == 8 and len(set(stability)) == 8
    assert {ids[i]["type"] for i in stability} == set(DOC["profiles"])
    assert {ids[i]["questionId"] for i in stability} == set(QUESTION_IDS)
