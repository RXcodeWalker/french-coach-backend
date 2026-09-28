"""Every authored 0520 question set in data/igcse/ passes the structural gate.

IgcseQuestionSetCreate (models/igcse.py) is what seed_igcse_questions.py runs
before it upserts anything, so a set that fails here would be skipped at seed
time. The rule tests below break exactly one structural rule on a known-good
set and assert the model rejects it - the backend mirror of the main repo's
src/data/exam/bank/__tests__/validate.test.ts.

Authoring-only pattern rules (two-part positions, echo-choice, ...) are
deliberately NOT here: they live in the main repo's patternLint.ts and are
checked by `npm run authoring:check`, never at seed or load time.
"""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.igcse import SUB_TOPICS_BY_AREA, IgcseQuestionSetCreate  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "igcse")
CURLY_APOSTROPHES = ("‘", "’", "ʼ", "´", "`")


def _files() -> list[str]:
    return sorted(f for f in os.listdir(DATA_DIR) if f.endswith(".json"))


def _load(filename: str) -> dict:
    with open(os.path.join(DATA_DIR, filename), "r", encoding="utf-8") as f:
        return json.load(f)


def test_there_are_ten_sets() -> None:
    assert len(_files()) == 10


@pytest.mark.parametrize("filename", _files())
def test_set_passes_the_structural_gate(filename: str) -> None:
    raw = _load(filename)
    validated = IgcseQuestionSetCreate.model_validate(raw)
    assert f"{validated.question_set_id}.json" == filename


@pytest.mark.parametrize("filename", _files())
def test_set_uses_straight_apostrophes(filename: str) -> None:
    with open(os.path.join(DATA_DIR, filename), "r", encoding="utf-8") as f:
        text = f.read()
    for ch in CURLY_APOSTROPHES:
        assert ch not in text, f"{filename} contains {ch!r}; use a straight ASCII apostrophe (content-authoring 13)"


# -- one broken rule per test, on a known-good set --------------------------


@pytest.fixture
def good() -> dict:
    return copy.deepcopy(_load(_files()[0]))


def _rejects(raw: dict, fragment: str) -> None:
    with pytest.raises(ValidationError) as exc:
        IgcseQuestionSetCreate.model_validate(raw)
    assert fragment in str(exc.value)


def test_good_fixture_is_valid(good: dict) -> None:
    IgcseQuestionSetCreate.model_validate(good)


def test_topic_area_slot_topic1_must_be_a_or_b(good: dict) -> None:
    topic = good["content"]["topic1"]
    topic["topicArea"] = "D"
    topic["subTopic"] = "Education"
    for q in topic["questions"]:
        q["topicArea"] = "D"
        q["subTopic"] = "Education"
    _rejects(good, "topic1.topicArea")


def test_topic_area_slot_topic2_must_be_c_d_or_e(good: dict) -> None:
    topic = good["content"]["topic2"]
    topic["topicArea"] = "B"
    topic["subTopic"] = "Leisure time"
    for q in topic["questions"]:
        q["topicArea"] = "B"
        q["subTopic"] = "Leisure time"
    _rejects(good, "topic2.topicArea")


def test_alternative_on_q1_q2_is_rejected(good: dict) -> None:
    good["content"]["topic1"]["questions"][1]["alternativeTexts"] = ["Une question plus simple ?"]
    _rejects(good, "must not carry an alternative (TN p.7)")


def test_roleplay_alternative_is_rejected(good: dict) -> None:
    good["content"]["rolePlay"]["tasks"][2]["alternativeTexts"] = ["Une autre question ?"]
    _rejects(good, "must not carry an alternative (TN p.6)")


def test_sub_topic_from_another_area_is_rejected(good: dict) -> None:
    topic = good["content"]["topic2"]
    area = topic["topicArea"]
    foreign = next(s for a, subs in SUB_TOPICS_BY_AREA.items() if a != area for s in subs)
    topic["subTopic"] = foreign
    for q in topic["questions"]:
        q["subTopic"] = foreign
    _rejects(good, "belongs to area")


def test_free_text_sub_topic_is_rejected(good: dict) -> None:
    topic = good["content"]["topic1"]
    topic["subTopic"] = "Everyday Life"
    for q in topic["questions"]:
        q["subTopic"] = "Everyday Life"
    _rejects(good, "is not a syllabus sub-topic")


def test_question_topic_area_must_agree_with_topic(good: dict) -> None:
    topic = good["content"]["topic1"]
    topic["questions"][0]["topicArea"] = "B" if topic["topicArea"] == "A" else "A"
    _rejects(good, "topicArea")


def test_question_sub_topic_must_agree_with_topic(good: dict) -> None:
    topic = good["content"]["topic1"]
    other = next(s for s in SUB_TOPICS_BY_AREA[topic["topicArea"]] if s != topic["subTopic"])
    topic["questions"][0]["subTopic"] = other
    _rejects(good, "disagrees with the topic's subTopic")


def test_topic_question_part_must_match_its_slot(good: dict) -> None:
    good["content"]["topic2"]["questions"][3]["part"] = "topic1"
    _rejects(good, 'topic2.questions[3].part must be "topic2"')


def test_topic_title_is_required(good: dict) -> None:
    del good["content"]["topic1"]["title"]
    _rejects(good, "title")


def test_examiner_register_is_required_and_closed(good: dict) -> None:
    good["content"]["rolePlay"]["examinerRegister"] = "vouvoiement"
    _rejects(good, "examinerRegister")
