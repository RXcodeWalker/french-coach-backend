"""Learn marking-level ladder scoring code (lib/learn_ladder.py; frontend docs/systems/learn-marking-levels.md).

Covers Batch 1's slice 1a: which ladders apply, the guardrails, the points, and the needs list, with
property tests (exhaustive over every combination, so no randomness) for the guarantees the design
claims by construction: A1 >= A2 >= Exam >= B1 >= B2, Exam between A2 and B1, the relevance cap, and an
empty needs list when the level is met. It also runs the Stage 0b dry run: the score gates that can be
computed from the pre-registered *expected* verdicts (tests/fixtures/learn_marking_eval_v1), using this
code. No model is called.

Run: pytest tests/test_learn_ladder.py
"""

from __future__ import annotations

import inspect
import itertools
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import learn_ladder as ll

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "learn_marking_eval_v1" / "answers.json").read_text(encoding="utf-8"))
DEMANDS_DIR = ROOT / "data" / "learn"

# Pinned on purpose: any change to the ladder wording, requirements, points or caps changes the version
# that every levelCheck records. Update the pin in the same commit as the change.
LADDER_VERSION_PIN = "learn-ladder-v1-8d123ddf9bcd"


def _corpus() -> dict[str, dict]:
    demands = {}
    for path in sorted(DEMANDS_DIR.glob("*.json")):
        for entry in json.loads(path.read_text(encoding="utf-8"))["entries"]:
            demands[entry["questionId"]] = entry["demands"]
    return demands


CORPUS = _corpus()
ALL_APPLIES = [
    tuple(ladder for ladder in ll.LADDER_ORDER if ladder in ("T", "L", "R", "A") or ladder in extra)
    for extra in ({}, {"O"}, {"F"}, {"O", "F"})
]


def _reached_combinations(applies):
    """Every reached rung (0-4) for every applicable ladder; O and F are at least 1 (their A1 rung is null)."""
    ranges = [range(1, 5) if ladder in ("O", "F") else range(0, 5) for ladder in applies]
    for combo in itertools.product(*ranges):
        yield dict(zip(applies, combo))


def _developed(transcript, n=3):
    """n valid ladder-T development pairs built from `transcript`'s words: the first word is the point,
    each later word one "more". Fewer when the transcript has fewer words."""
    words = transcript.split()
    return [{"point": words[0], "more": more, "how": "why"} for more in words[1:n + 1]]


DEV_TRANSCRIPT = "un deux trois quatre cinq"


def _raw_from_reached(reached, transcript="x"):
    """A model reply whose verdicts give exactly `reached`, quoting a word that is in `transcript`, with
    enough T development evidence for the T rungs it claims."""
    raw = {}
    for ladder, rung in reached.items():
        verdicts = ["y" if i < rung else "n" for i in range(4)]
        if ladder in ("O", "F"):
            verdicts[0] = None
        raw[ladder] = {"verdicts": verdicts, "quote": transcript, "next": ""}
    if "T" in raw:
        raw["T"]["developed"] = _developed(transcript)
    return raw


# ── Which ladders apply ──────────────────────────────────────────────────────────────────────

def test_applicable_ladders_for_the_five_test_questions():
    assert {q: ll.applicable_ladders(CORPUS[q]) for q in ("fam_01", "hol_01", "sch_03", "env_05", "fut_19")} == {
        "fam_01": ("T", "L", "R", "A"),
        "hol_01": ("T", "L", "F", "R", "A"),
        "sch_03": ("T", "L", "R", "A"),
        "env_05": ("T", "L", "O", "R", "A"),
        "fut_19": ("T", "L", "O", "F", "R", "A"),
    }


def test_unresolved_demands_apply_only_the_four_always_ladders():
    assert ll.applicable_ladders(None) == ("T", "L", "R", "A")
    assert ll.applicable_ladders({}) == ("T", "L", "R", "A")
    assert ll.f_parts(None) == ()


@pytest.mark.parametrize(
    "demands, expected",
    [
        ({"cognitiveDemand": "explain", "timeFrames": ["present"], "structures": []}, ("T", "L", "R", "A")),
        ({"cognitiveDemand": "describe", "timeFrames": ["present"], "structures": ["opinion"]}, ("T", "L", "O", "R", "A")),
        ({"cognitiveDemand": "describe", "timeFrames": ["present"], "structures": ["justification"]}, ("T", "L", "O", "R", "A")),
        ({"cognitiveDemand": "describe", "timeFrames": ["present"], "structures": ["comparison"]}, ("T", "L", "O", "R", "A")),
        ({"cognitiveDemand": "justify", "timeFrames": [], "structures": []}, ("T", "L", "O", "R", "A")),
        ({"cognitiveDemand": "compare", "timeFrames": [], "structures": []}, ("T", "L", "O", "R", "A")),
        ({"cognitiveDemand": "hypothesize", "timeFrames": [], "structures": []}, ("T", "L", "O", "R", "A")),
        ({"cognitiveDemand": "describe", "timeFrames": ["past"], "structures": []}, ("T", "L", "F", "R", "A")),
        ({"cognitiveDemand": "describe", "timeFrames": ["future"], "structures": []}, ("T", "L", "F", "R", "A")),
        ({"cognitiveDemand": "describe", "timeFrames": ["conditional"], "structures": []}, ("T", "L", "F", "R", "A")),
        ({"cognitiveDemand": "describe", "timeFrames": ["present"], "structures": ["perfect"]}, ("T", "L", "R", "A")),
    ],
)
def test_applicability_rule(demands, expected):
    assert ll.applicable_ladders(demands) == expected


def test_f_parts_only_the_parts_the_question_asks_for():
    assert ll.f_parts({"timeFrames": ["past"]}) == ("past",)
    assert ll.f_parts({"timeFrames": ["future"]}) == ("future",)
    assert ll.f_parts({"timeFrames": ["conditional"]}) == ("future",)
    assert ll.f_parts({"timeFrames": ["future", "conditional"]}) == ("future",)  # one part, either verb form
    assert ll.f_parts({"timeFrames": ["present", "past", "future"]}) == ("past", "future")


def test_every_question_in_the_corpus_gets_a_consistent_ladder_set():
    assert len(CORPUS) == 668
    for question_id, demands in CORPUS.items():
        applies = ll.applicable_ladders(demands)
        assert applies[:2] == ("T", "L") and applies[-2:] == ("R", "A"), question_id
        assert ("O" in applies) == ll.invites_opinion(demands), question_id
        assert ("F" in applies) == bool(ll.f_parts(demands)), question_id
        assert list(applies) == [ladder for ladder in ll.LADDER_ORDER if ladder in applies], question_id


# ── Requirements and points ──────────────────────────────────────────────────────────────────

def test_exam_standard_requirements_follow_the_spec_table():
    assert {ladder: ll.required_rung(ladder, "exam") for ladder in ll.LADDER_ORDER} == {
        "L": 3, "O": 3, "F": 3, "A": 3,  # B1
        "T": 2, "R": 2,  # A2
    }
    for level, rung in zip(ll.LEVELS, (1, 2, 3, 4)):
        assert all(ll.required_rung(ladder, level) == rung for ladder in ll.LADDER_ORDER)


@pytest.mark.parametrize("ladder", ll.LADDER_ORDER)
def test_requirement_never_drops_and_exam_sits_between_a2_and_b1(ladder):
    required = [ll.required_rung(ladder, level) for level in ll.MARKING_LEVELS]
    assert required == sorted(required)
    assert ll.required_rung(ladder, "A2") <= ll.required_rung(ladder, "exam") <= ll.required_rung(ladder, "B1")


def test_points_table():
    assert ll.ladder_points(reached=4, required=3) == 10  # above
    assert ll.ladder_points(reached=3, required=3) == 8  # meets
    assert ll.ladder_points(reached=4, required=4) == 10  # meets the top rung
    assert ll.ladder_points(reached=2, required=3) == 5
    assert ll.ladder_points(reached=1, required=3) == 2.5
    assert ll.ladder_points(reached=0, required=3) == 1
    assert ll.ladder_points(reached=0, required=4) == 1
    assert ll.ladder_points(reached=1, required=1) == 8


def test_points_never_rise_with_the_requirement_and_never_fall_with_the_rung_reached():
    for reached in range(0, 5):
        by_required = [ll.ladder_points(reached, required) for required in range(1, 5)]
        assert by_required == sorted(by_required, reverse=True), reached
    for required in range(1, 5):
        by_reached = [ll.ladder_points(reached, required) for reached in range(0, 5)]
        assert by_reached == sorted(by_reached), required


# ── Properties of the whole score ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("applies", ALL_APPLIES, ids=lambda a: "".join(a))
def test_scores_fall_a1_to_b2_with_exam_between_a2_and_b1_for_every_combination(applies):
    count = 0
    for reached in _reached_combinations(applies):
        scores = ll.sub_scores_all_levels(reached, applies)
        for key in ll.SUB_SCORE_KEYS:
            series = [scores[level][key] for level in ll.MARKING_LEVELS]
            assert series == sorted(series, reverse=True), (reached, key, series)
        overalls = [ll.overall_from_sub_scores(scores[level]) for level in ll.MARKING_LEVELS]
        assert overalls == sorted(overalls, reverse=True), (reached, overalls)
        a2, exam, b1 = overalls[1], overalls[2], overalls[3]
        assert a2 >= exam >= b1
        count += 1
    assert count == (5 ** 4) * (4 ** (len(applies) - 4))


@pytest.mark.parametrize("applies", ALL_APPLIES, ids=lambda a: "".join(a))
def test_relevance_cap_holds_at_every_level_when_t_a1_is_not_met(applies):
    for reached in _reached_combinations(applies):
        scores = ll.sub_scores_all_levels(reached, applies)
        for level in ll.MARKING_LEVELS:
            for key in ll.SUB_SCORE_KEYS:
                if reached["T"] == 0:
                    assert scores[level][key] <= ll.RELEVANCE_CAP
        # The cap is the only thing that depends on T-A1: an answer that meets it is never capped by it.
        if reached["T"] >= 1 and all(r == 4 for r in reached.values()):
            assert all(scores[level][key] == 10 for level in ll.MARKING_LEVELS for key in ll.SUB_SCORE_KEYS)


def test_comm_is_the_mean_of_the_applicable_t_o_f_points():
    reached = {"T": 4, "L": 1, "O": 2, "F": 1, "R": 1, "A": 1}
    scores = ll.sub_scores_all_levels(reached, ("T", "L", "O", "F", "R", "A"))
    # at B1 (required rung 3): T 4 -> 10, O 2 -> 5, F 1 -> 2.5
    assert scores["B1"]["comm"] == pytest.approx((10 + 5 + 2.5) / 3)
    # without O and F only T counts
    only_t = ll.sub_scores_all_levels({"T": 2, "L": 1, "R": 1, "A": 1}, ("T", "L", "R", "A"))
    assert only_t["B1"]["comm"] == 5
    assert only_t["B1"]["fluency"] == 2.5 and only_t["B1"]["know"] == 2.5 and only_t["B1"]["acc"] == 2.5


def test_sub_scores_are_not_rounded():
    reached = {"T": 4, "L": 1, "O": 2, "F": 1, "R": 1, "A": 1}
    comm = ll.sub_scores_all_levels(reached, ("T", "L", "O", "F", "R", "A"))["B1"]["comm"]
    assert comm != round(comm, 1)


def test_overall_matches_the_clients_compute_overall():
    assert ll.overall_from_sub_scores({"comm": 10, "fluency": 8, "know": 10, "acc": 10}) == 9.5
    assert ll.overall_from_sub_scores({"comm": 7.95, "fluency": 7.95, "know": 7.95, "acc": 7.95}) == 8.0  # half up
    assert ll.overall_from_sub_scores({"comm": 5, "fluency": 5, "know": 5, "acc": 8}) == 5.8  # 5.75 -> 5.8
    assert ll.overall_from_sub_scores({"comm": 1, "fluency": 1, "know": 1, "acc": 1}) == 1.0


def test_cefr_level_is_the_highest_level_with_overall_at_least_8_and_never_exam():
    def sub(overall):
        return {"comm": overall, "fluency": overall, "know": overall, "acc": overall}

    def by_level(*overalls):
        return dict(zip(ll.MARKING_LEVELS, (sub(o) for o in overalls)))

    assert ll.derive_cefr_level(by_level(10, 10, 10, 10, 10)) == "B2"
    assert ll.derive_cefr_level(by_level(10, 10, 9.5, 8.0, 7.9)) == "B1"
    assert ll.derive_cefr_level(by_level(10, 8.0, 7.0, 5, 3)) == "A2"
    assert ll.derive_cefr_level(by_level(9, 7.9, 6, 4, 2)) == "A1"
    assert ll.derive_cefr_level(by_level(5, 4, 3, 2, 1)) == "A1"  # none reaches 8: A1, not a made-up level
    for _ in range(2):
        assert ll.derive_cefr_level(by_level(10, 10, 10, 10, 10)) in ll.LEVELS


def test_the_scorer_takes_no_marking_level():
    """ADR 0013 #2: the level never reaches the backend, so nothing here can be influenced by it."""
    for function in (ll.score_ladders, ll.apply_guardrails, ll.sub_scores_all_levels, ll.needs_by_level, ll.derive_cefr_level):
        names = set(inspect.signature(function).parameters)
        assert not names & {"level", "marking_level", "strictness", "tier", "target_level"}, function.__name__


# ── Quote matching ───────────────────────────────────────────────────────────────────────────

TRANSCRIPT = "Dans ma famille, il y a quatre personnes : mon père, ma mère, mon frère et moi. J’aime l’école, mais c'est dur."


@pytest.mark.parametrize(
    "quote",
    [
        "mon frère et moi",
        "MON FRERE ET MOI",  # case and accents
        "mon frere   et  moi",  # spacing
        "J'aime l'école",  # apostrophe style (typographic in the transcript, straight in the quote)
        "j’aime l’ecole",
        "quatre personnes: mon père",  # punctuation and French spacing before the colon
        "il y a … mon père",  # each fragment is checked on its own
        "il y a ... ma mère",
        "dur",
    ],
)
def test_quotes_that_are_in_the_transcript_are_found(quote):
    assert ll.quote_in_transcript(quote, TRANSCRIPT)


@pytest.mark.parametrize(
    "quote",
    [
        "mon grand frère",  # invented
        "mon père … ma tante",  # one fragment is not there
        "",
        "   ",
        "…",
        None,
        42,
        "fam",  # inside a word is not the word
        "personne",  # not verbatim ("personnes")
    ],
)
def test_quotes_that_are_not_in_the_transcript_are_not_found(quote):
    assert not ll.quote_in_transcript(quote, TRANSCRIPT)


# ── Guardrails ───────────────────────────────────────────────────────────────────────────────

# Three valid development pairs in TRANSCRIPT, so a T rung 3-4 "yes" in a test is not dropped for want
# of evidence (tests of that rule pass their own).
TRANSCRIPT_DEVELOPED = [
    {"point": "mon père", "more": more, "how": "example"} for more in ("ma mère", "mon frère", "J'aime l'école")
]


def _entry(verdicts, quote="mon frère et moi", nxt="", developed=None):
    return {"verdicts": verdicts, "quote": quote, "next": nxt,
            "developed": TRANSCRIPT_DEVELOPED if developed is None else developed}


def _all_n_raw(applies, **overrides):
    raw = {ladder: _entry(["n"] * 4, quote="") for ladder in applies}
    for ladder in ("O", "F"):
        if ladder in raw:
            raw[ladder]["verdicts"][0] = None
    raw.update(overrides)
    return raw


FOUR = ("T", "L", "R", "A")


def test_a_yes_above_a_no_is_ignored():
    raw = _all_n_raw(FOUR, T=_entry(["y", "n", "y", "y"]))
    guarded = ll.apply_guardrails(raw, FOUR, TRANSCRIPT)
    assert guarded["T"].reached == 1


def test_a_yes_whose_quote_is_not_in_the_transcript_becomes_a_no():
    raw = _all_n_raw(FOUR, T=_entry(["y", "y", "y", "n"], quote="une phrase inventée"))
    guarded = ll.apply_guardrails(raw, FOUR, TRANSCRIPT)["T"]
    assert guarded.quote_found is False
    assert guarded.verdicts == ("y", "y", "n", "n")
    assert guarded.reached == 2


def test_a_missing_quote_fails_the_highest_yes_too():
    raw = _all_n_raw(FOUR, T=_entry(["y", "y", "n", "n"], quote=None))
    assert ll.apply_guardrails(raw, FOUR, TRANSCRIPT)["T"].reached == 1


def test_a_valid_quote_keeps_every_yes():
    raw = _all_n_raw(FOUR, T=_entry(["y", "y", "y", "n"]))
    guarded = ll.apply_guardrails(raw, FOUR, TRANSCRIPT)["T"]
    assert guarded.quote_found is True and guarded.reached == 3


def test_a_ladder_with_no_yes_needs_no_quote():
    guarded = ll.apply_guardrails(_all_n_raw(FOUR), FOUR, TRANSCRIPT)
    assert all(g.quote_found is None for g in guarded.values())
    assert all(g.reached == 0 for g in guarded.values())


@pytest.mark.parametrize(
    "developed, reached",
    [
        ([], 2),  # no evidence: neither development rung stands
        (None, 4),  # three valid pairs
        (TRANSCRIPT_DEVELOPED[:1], 3),  # one pair: rung 3 stands, rung 4 needs three
        (TRANSCRIPT_DEVELOPED[:2], 3),
        ([{"point": "mon père", "more": "des mots inventés", "how": "why"}] * 3, 2),  # not in the transcript
        ([{"point": "mon père", "more": "mon père", "how": "why"}], 2),  # the same words
        ([{"point": "mon père", "more": "ma mère", "how": "because"}], 2),  # an unknown kind
        ([dict(TRANSCRIPT_DEVELOPED[0])] * 3, 3),  # the same "more" counts once
        ("not a list", 2),
        ([None, "x", 3], 2),
    ],
)
def test_t_development_rungs_need_their_evidence(developed, reached):
    raw = _all_n_raw(FOUR, T=_entry(["y", "y", "y", "y"], developed=developed))
    assert ll.apply_guardrails(raw, FOUR, TRANSCRIPT)["T"].reached == reached


def test_development_evidence_only_matters_on_ladder_t():
    raw = _all_n_raw(FOUR, L=_entry(["y", "y", "y", "y"], developed=[]))
    assert ll.apply_guardrails(raw, FOUR, TRANSCRIPT)["L"].reached == 4


def test_the_development_minimums():
    assert (ll.T_B1_MIN_DEVELOPED, ll.T_B2_MIN_DEVELOPED) == (1, 3)
    assert ll.DEVELOPMENT_KINDS == {"why", "how", "example", "result", "feeling"}


def test_a_null_counts_as_no_except_on_the_a1_rung_of_o_and_f():
    raw = _all_n_raw(FOUR, T=_entry(["y", None, "y", "y"]))
    assert ll.apply_guardrails(raw, FOUR, TRANSCRIPT)["T"].reached == 1
    # The first rung of T, L, R, A is a real requirement: a null there is a "no".
    raw = _all_n_raw(FOUR, L=_entry([None, "y", "y", "y"]))
    assert ll.apply_guardrails(raw, FOUR, TRANSCRIPT)["L"].reached == 0
    applies = ("T", "L", "O", "F", "R", "A")
    raw = _all_n_raw(applies, O=_entry([None, "y", "n", "n"]), F=_entry([None, "n", "n", "n"], quote=""))
    guarded = ll.apply_guardrails(raw, applies, TRANSCRIPT)
    assert guarded["O"].reached == 2
    assert guarded["F"].reached == 1  # the missing A1 rung counts as met


@pytest.mark.parametrize("written", ["null", "NULL", " null "])
def test_the_string_null_is_read_as_json_null(written):
    # A contract fix, not a scoring rule: the first gate run lost fut_19_b2 because the model wrote
    # "null" in quotes on the A1 rung of O and F. It now follows the usual null rules.
    applies = ("T", "L", "O", "F", "R", "A")
    raw = _all_n_raw(
        applies, O=_entry([written, "y", "n", "n"]), F=_entry([written, "y", "n", "n"]), R=_entry([written, "y", "n", "n"])
    )
    ll.validate_raw_levelcheck(raw, applies)  # no longer a contract error
    guarded = ll.apply_guardrails(raw, applies, TRANSCRIPT)
    assert guarded["O"].reached == 2 and guarded["F"].reached == 2  # met: the A1 rung has no requirement
    assert guarded["R"].reached == 0  # R's A1 rung is a real requirement: a null there is a "no"


def test_other_strings_are_still_a_contract_error():
    for bad in ("maybe", "None", "yes", ""):
        raw = _all_n_raw(FOUR, T=_entry([bad, "n", "n", "n"], quote=""))
        with pytest.raises(ll.LadderValidationError):
            ll.validate_raw_levelcheck(raw, FOUR)


def test_the_a1_rung_of_o_and_f_is_met_whatever_the_model_wrote():
    applies = ("T", "L", "O", "F", "R", "A")
    raw = _all_n_raw(applies, O=_entry(["n", "n", "n", "n"], quote=""), F=_entry(["y", "n", "n", "n"]))
    guarded = ll.apply_guardrails(raw, applies, TRANSCRIPT)
    assert guarded["O"].verdicts[0] is None and guarded["F"].verdicts[0] is None
    assert guarded["O"].reached == 1 and guarded["F"].reached == 1
    assert guarded["F"].quote_found is None  # the ignored "y" at A1 needs no evidence


def test_a_ladder_that_does_not_apply_is_ignored_even_if_the_model_sent_it():
    raw = _all_n_raw(FOUR, O=_entry([None, "y", "y", "y"]))
    assert set(ll.apply_guardrails(raw, FOUR, TRANSCRIPT)) == set(FOUR)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "T",
        [],
        {},
        {"T": _entry(["y", "y", "n", "n"]), "L": _entry(["y"] * 4), "R": _entry(["y"] * 4)},  # A missing
        {**{k: _entry(["y"] * 4) for k in FOUR}, "T": _entry(["y", "y", "n"])},  # three verdicts
        {**{k: _entry(["y"] * 4) for k in FOUR}, "T": _entry(["y", "y", "maybe", "n"])},
        {**{k: _entry(["y"] * 4) for k in FOUR}, "T": _entry(["y", "y", True, "n"])},
        {**{k: _entry(["y"] * 4) for k in FOUR}, "T": _entry("yynn")},
        {**{k: _entry(["y"] * 4) for k in FOUR}, "T": ["y", "y", "n", "n"]},
    ],
)
def test_a_missing_or_malformed_levelcheck_raises_so_the_provider_fallback_runs(raw):
    with pytest.raises(ll.LadderValidationError):
        ll.apply_guardrails(raw, FOUR, TRANSCRIPT)
    assert issubclass(ll.LadderValidationError, ValueError)


# ── Needs list ───────────────────────────────────────────────────────────────────────────────

SIX = ("T", "L", "O", "F", "R", "A")


def _needs_for(reached, transcript=DEV_TRANSCRIPT, demands=None, **overrides):
    applies = tuple(reached)
    raw = _raw_from_reached(reached, transcript)
    for ladder, entry in overrides.items():
        raw[ladder].update(entry)
    guarded = ll.apply_guardrails(raw, applies, transcript, demands)
    return ll.needs_by_level(guarded, applies, demands)


def test_needs_is_empty_when_the_level_is_met_for_every_combination():
    for applies in ALL_APPLIES:
        for reached in _reached_combinations(applies):
            needs = _needs_for(reached)
            for level in ll.MARKING_LEVELS:
                below = [l for l in applies if reached[l] < ll.required_rung(l, level)]
                assert [item["ladder"] for item in needs[level]] == [l for l in ll.NEEDS_ORDER if l in below][: ll.MAX_NEEDS_ITEMS]
                if not below:
                    assert needs[level] == []


def test_a_top_answer_needs_nothing_at_any_level():
    needs = _needs_for(dict.fromkeys(SIX, 4))
    assert all(items == [] for items in needs.values())


def test_needs_are_ordered_t_o_f_l_a_r_and_capped_at_three():
    needs = _needs_for(dict.fromkeys(SIX, 1))
    assert [i["ladder"] for i in needs["B2"]] == ["T", "O", "F"]
    needs = _needs_for({"T": 4, "L": 1, "O": 4, "F": 4, "R": 1, "A": 1})
    assert [i["ladder"] for i in needs["B1"]] == ["L", "A", "R"]


def test_needs_names_the_next_rung_and_flags_a_first_step_when_two_or_more_short():
    items = _needs_for({"T": 2, "L": 1, "R": 2, "A": 3})["B1"]
    by_ladder = {i["ladder"]: i for i in items}
    assert by_ladder["T"]["nextRung"] == "B1" and by_ladder["T"]["firstStep"] is False  # reached A2, needs B1
    assert by_ladder["L"]["nextRung"] == "A2" and by_ladder["L"]["firstStep"] is True  # reached A1, needs B1
    assert by_ladder["L"]["text"] == ll.rung_text("L", 1, None, "learner")
    assert by_ladder["R"]["nextRung"] == "B1"
    assert "A" not in by_ladder  # reached B1 on accuracy: nothing missing


def test_needs_at_each_level_is_exactly_the_unreached_rungs_of_that_level():
    needs = _needs_for({"T": 3, "L": 2, "R": 2, "A": 4})
    assert [i["ladder"] for i in needs["A1"]] == []
    assert [i["ladder"] for i in needs["A2"]] == []
    assert [i["ladder"] for i in needs["exam"]] == ["L"]
    assert [i["ladder"] for i in needs["B1"]] == ["L", "R"]
    assert [i["ladder"] for i in needs["B2"]] == ["T", "L", "R"]


def test_needs_when_t_a1_is_not_met_starts_at_a1():
    items = _needs_for({"T": 0, "L": 4, "R": 4, "A": 4})["A1"]
    assert items == [
        {
            "ladder": "T", "label": "Developing your answer", "nextRung": "A1",
            "text": ll.rung_text("T", 0, None, "learner"), "firstStep": False, "example": None,
        }
    ]


def test_an_example_is_kept_when_it_is_short_new_and_for_the_next_rung():
    good = "Ma mère est gentille mais un peu stricte"
    needs = _needs_for({"T": 4, "L": 1, "R": 4, "A": 4}, transcript="mon frère et moi", L={"next": good})
    assert needs["B1"][0]["example"] == good


@pytest.mark.parametrize(
    "example",
    [
        None,
        "",
        "   ",
        42,
        " ".join(["mot"] * 21),  # over 20 words
        "mon frère et moi",  # the student's own words back
        "Frère et moi",  # a part of the transcript
        "Use simple connectors like mais, parce que or car.",  # the learner sentence, copied
    ],
)
def test_a_bad_example_is_dropped_never_rewritten(example):
    needs = _needs_for({"T": 4, "L": 1, "R": 4, "A": 4}, transcript="mon frère et moi", L={"next": example})
    assert needs["B1"][0]["example"] is None
    assert needs["B1"][0]["ladder"] == "L"  # the item itself stays


def test_an_example_of_exactly_twenty_words_is_kept():
    twenty = " ".join(f"mot{i}" for i in range(20))
    needs = _needs_for({"T": 4, "L": 1, "R": 4, "A": 4}, transcript="mon frère et moi", L={"next": twenty})
    assert needs["B1"][0]["example"] == twenty


def test_an_example_written_for_a_rung_the_guardrail_moved_is_dropped():
    raw = _raw_from_reached({"T": 4, "L": 4, "R": 4, "A": 4}, transcript="mon frère et moi")
    raw["L"] = {"verdicts": ["y", "y", "y", "n"], "quote": "une phrase inventée", "next": "Il pleut donc je reste à la maison"}
    guarded = ll.apply_guardrails(raw, FOUR, "mon frère et moi")
    assert guarded["L"].reached == 2  # B1 was demoted; the model's example was for the B2 rung
    assert guarded["L"].example is None


def test_a_bad_example_never_changes_a_score():
    reached = {"T": 4, "L": 1, "R": 4, "A": 4}
    plain = ll.score_ladders(_raw_from_reached(reached, "mon frère et moi"), None, "mon frère et moi")
    raw = _raw_from_reached(reached, "mon frère et moi")
    raw["L"]["next"] = "Il pleut donc je reste à la maison"
    with_example = ll.score_ladders(raw, None, "mon frère et moi")
    assert plain.level_check["subScoresByLevel"] == with_example.level_check["subScoresByLevel"]


# ── Gate G14: only what the question asks, over all 668 questions ───────────────────────────

# Words that would ask for something a ladder may not ask for. Checked on the learner's sentences.
_ASKS_OPINION = ("opinion", "think", "prefer", "reason", "because", "why", "view")
_ASKS_PAST = ("past", "felt", "reacted", "happened", "meant to you", "experience", "allé", "visité")
_ASKS_FUTURE = ("future", "conditional", "plan", "wish", "hope", "imagined", "speculate", "could happen", "voudrais", "aimerais")
_NAMES_A_TENSE = ("past", "future", "conditional", "imperfect", "pluperfect", "passé", "perfect")


def _texts(items):
    return [(item["ladder"], item["text"].lower()) for item in items]


def test_g14_no_needs_item_asks_for_a_time_frame_opinion_or_reason_the_question_does_not_invite():
    checked = 0
    for question_id, demands in CORPUS.items():
        applies = ll.applicable_ladders(demands)
        parts = ll.f_parts(demands)
        reached = dict.fromkeys(applies, 0)
        for ladder in ("O", "F"):
            if ladder in reached:
                reached[ladder] = 1
        needs = _needs_for(reached, demands=demands)  # worst case: every ladder as low as it can be
        for level, items in needs.items():
            for ladder, text in _texts(items):
                checked += 1
                assert ladder in applies, (question_id, level, ladder)
                if ladder == "O":
                    continue
                if ladder != "F":
                    # T, L, R and A never ask for an opinion, a reason, a time frame or a particular tense.
                    for word in _ASKS_OPINION + _ASKS_PAST + _ASKS_FUTURE:
                        if ladder == "L" and word in ("because",):
                            continue
                        assert word not in text, (question_id, level, ladder, word, text)
                    if ladder in ("T", "L", "R"):
                        for word in _NAMES_A_TENSE:
                            assert word not in text, (question_id, level, ladder, word, text)
                    continue
                if "past" not in parts:
                    for word in _ASKS_PAST:
                        assert word not in text, (question_id, level, word, text)
                if "future" not in parts:
                    for word in _ASKS_FUTURE:
                        assert word not in text, (question_id, level, word, text)
        # O-A2 asks for a reason only when the question asks why.
        if "O" in applies:
            text = ll.rung_text("O", 1, demands, "learner").lower()
            assert ("reason" in text) == ll.asks_why(demands), question_id
    assert checked > 668


def test_g14_wording_for_the_five_test_questions():
    hol = ll.rung_text("F", 1, CORPUS["hol_01"], "learner")
    assert "past" in hol and "future" not in hol and "plan" not in hol
    fut = ll.rung_text("F", 1, CORPUS["fut_19"], "learner")
    assert "future" in fut.lower() and "past" not in fut.lower()
    assert "reason" in ll.rung_text("O", 1, CORPUS["fut_19"], "learner")  # 'Pourquoi ?'
    assert "reason" not in ll.rung_text("O", 1, CORPUS["env_05"], "learner")
    assert ll.ladder_label("F", CORPUS["hol_01"]) == "Talking about the past"
    assert ll.ladder_label("F", CORPUS["fut_19"]) == "Talking about plans and wishes"
    assert ll.ladder_label("F", {"timeFrames": ["past", "future"]}) == "Talking about the past and the future"


def test_a_question_asking_for_both_parts_needs_both():
    demands = {"cognitiveDemand": "describe", "timeFrames": ["past", "future"], "structures": []}
    learner = ll.rung_text("F", 1, demands, "learner")
    assert "past" in learner and "future" in learner
    model = ll.rung_text("F", 1, demands, "model")
    assert model.startswith("Both parts must be met.") and "Past part:" in model and "Future or imagined part:" in model
    assert ll.rung_text("F", 0, demands, "model") is None


def test_the_a1_rung_of_o_and_f_has_no_wording():
    assert ll.rung_text("O", 0, CORPUS["env_05"], "model") is None
    assert ll.rung_text("F", 0, CORPUS["hol_01"], "learner") is None
    for ladder in ("T", "L", "R", "A"):
        assert ll.rung_text(ladder, 0, None, "model")


def test_rung_text_rejects_an_unknown_audience():
    with pytest.raises(ValueError):
        ll.rung_text("T", 0, None, "teacher")


def test_model_wording_names_no_marking_level():
    """The ladder is level-blind (ADR 0013 #2): nothing the judge reads says what level the learner picked."""
    for ladder in ll.LADDER_ORDER:
        for demands in (None, CORPUS["fut_19"], CORPUS["hol_01"], CORPUS["env_05"], {"timeFrames": ["past", "future"]}):
            for index in range(4):
                text = (ll.rung_text(ladder, index, demands, "model") or "").lower()
                for banned in ("target level", "marking level", "exam standard", "strict", "lenient", "learner picked"):
                    assert banned not in text, (ladder, index, banned)
                assert not re.search(r"\b(?:a1|a2|b1|b2|c1|c2)\b", text), (ladder, index, "a CEFR level")


def test_version_is_pinned():
    assert ll.LEARN_LADDER_VERSION == LADDER_VERSION_PIN, (
        f"the ladder wording, requirements, points or caps changed: update LADDER_VERSION_PIN to {ll.LEARN_LADDER_VERSION}"
    )


# ── score_ladders ────────────────────────────────────────────────────────────────────────────

def test_score_ladders_returns_the_levelcheck_the_spec_describes():
    demands = CORPUS["env_05"]
    reached = {"T": 3, "L": 4, "O": 3, "R": 3, "A": 4}
    result = ll.score_ladders(_raw_from_reached(reached, DEV_TRANSCRIPT), demands, DEV_TRANSCRIPT)
    check = result.level_check
    assert set(check) == {"scheme", "ladderVersion", "applies", "reached", "subScoresByLevel", "needsByLevel"}
    assert check["scheme"] == "ladder-v1" and check["ladderVersion"] == ll.LEARN_LADDER_VERSION
    assert check["applies"] == ["T", "L", "O", "R", "A"]
    assert check["reached"] == {"T": "B1", "L": "B2", "O": "B1", "R": "B1", "A": "B2"}
    assert list(check["subScoresByLevel"]) == ["A1", "A2", "exam", "B1", "B2"]
    assert set(check["subScoresByLevel"]["exam"]) == {"comm", "fluency", "know", "acc"}
    assert list(check["needsByLevel"]) == ["A1", "A2", "exam", "B1", "B2"]
    assert result.answered_the_question is True
    json.dumps(check)  # plain data: goes straight into the reply


def test_score_ladders_reports_reached_none_and_not_answered_when_t_a1_fails():
    raw = _raw_from_reached({"T": 0, "L": 4, "O": 1, "R": 4, "A": 4}, "x")
    result = ll.score_ladders(raw, CORPUS["env_05"], "x")
    assert result.level_check["reached"]["T"] == "none"
    assert result.answered_the_question is False
    assert all(v <= 2 for level in result.level_check["subScoresByLevel"].values() for v in level.values())


def test_a_fabricated_quote_on_t_a1_marks_the_answer_off_topic():
    raw = _raw_from_reached({"T": 1, "L": 1, "R": 1, "A": 1}, "une phrase inventée")
    result = ll.score_ladders(raw, None, "Dans ma famille il y a quatre personnes")
    assert result.answered_the_question is False
    assert result.level_check["reached"]["T"] == "none"


def test_score_ladders_unresolved_demands_scores_four_ladders():
    result = ll.score_ladders(_raw_from_reached(dict.fromkeys(FOUR, 2), "x"), None, "x")
    assert result.level_check["applies"] == list(FOUR)


def test_score_ladders_raises_on_a_bad_levelcheck():
    with pytest.raises(ll.LadderValidationError):
        ll.score_ladders({}, CORPUS["fam_01"], "x")


def test_score_ladders_is_deterministic():
    raw = _raw_from_reached({"T": 2, "L": 1, "R": 2, "A": 3}, "x")
    first = ll.score_ladders(raw, CORPUS["fam_01"], "x").level_check
    second = ll.score_ladders(json.loads(json.dumps(raw)), CORPUS["fam_01"], "x").level_check
    assert first == second


# ── Stage 0b: the score gates, computed from the pre-registered expected verdicts ───────────
# Dry run at authoring time (fixture README): the table below is the one the fixture's author computed
# by hand-run arithmetic. The real code must reproduce it, or either the code or the fixture is wrong.

EXPECTED_OVERALLS = {
    "fam_01_simple_a1": (9.5, 7.8, 7.1, 5.6, 4.0),
    "fam_01_typical_a2": (10.0, 8.5, 7.3, 5.8, 3.1),
    "fam_01_strong_b1": (10.0, 10.0, 9.5, 8.5, 6.3),
    "fam_01_weak": (9.5, 7.3, 5.9, 4.4, 2.1),
    "hol_01_simple_a1": (9.5, 7.8, 6.8, 5.6, 4.0),
    "hol_01_typical_a2": (10.0, 8.5, 6.9, 5.8, 3.1),
    "hol_01_strong_b1": (10.0, 10.0, 9.3, 8.5, 6.3),
    "hol_01_weak": (9.5, 7.3, 5.5, 4.4, 2.1),
    "sch_03_simple_a1": (9.5, 7.8, 7.1, 5.6, 4.0),
    "sch_03_typical_a2": (10.0, 8.5, 7.3, 5.8, 3.1),
    "sch_03_strong_b1": (10.0, 10.0, 9.5, 8.5, 6.3),
    "sch_03_weak": (9.5, 7.3, 5.9, 4.4, 2.1),
    "env_05_simple_a1": (9.5, 7.8, 6.8, 5.6, 4.0),
    "env_05_typical_a2": (10.0, 8.8, 7.3, 6.1, 3.4),
    "env_05_strong_b1": (10.0, 10.0, 9.8, 9.0, 7.5),
    "env_05_weak": (9.5, 7.3, 5.5, 4.4, 2.1),
    "env_05_off_topic": (2.0, 2.0, 2.0, 1.9, 1.5),
    "fut_19_simple_a1": (9.3, 7.5, 6.4, 5.4, 3.9),
    "fut_19_typical_a2": (10.0, 8.7, 7.0, 6.0, 3.3),
    "fut_19_strong_b1": (10.0, 10.0, 9.2, 8.5, 6.3),
    "fut_19_weak": (9.8, 7.9, 6.0, 5.0, 2.6),
    "fut_19_b2": (10.0, 10.0, 10.0, 10.0, 10.0),
}


def sentence_developed(transcript):
    """Three development pairs from the answer's own sentences (the first as the point), so an expected
    T rung 3-4 "yes" carries the evidence the guardrail asks for."""
    sentences = [x for x in re.split(r"(?<=[.!?])\s+", transcript) if x]
    return [{"point": sentences[0], "more": more, "how": "example"} for more in sentences[1:4]]


def _expected_raw(answer):
    """The model reply the pre-registered expectations amount to."""
    raw = {}
    for ladder, expected in answer["ladders"].items():
        raw[ladder] = {"verdicts": expected["verdicts"], "quote": expected.get("evidence", ""), "next": ""}
        if ladder == "T":
            raw[ladder]["developed"] = sentence_developed(answer["transcript"])
    return raw


def _score_expected(answer):
    demands = CORPUS[answer["questionId"]]
    return ll.score_ladders(_expected_raw(answer), demands, answer["transcript"])


def _overalls(result):
    return {level: ll.overall_from_sub_scores(result.level_check["subScoresByLevel"][level]) for level in ll.MARKING_LEVELS}


ANSWERS = FIXTURE["answers"]
BY_TYPE = lambda kind: [a for a in ANSWERS if a["type"] == kind]  # noqa: E731


def test_the_dry_run_table_covers_every_answer():
    assert set(EXPECTED_OVERALLS) == {a["id"] for a in ANSWERS}


@pytest.mark.parametrize("answer", ANSWERS, ids=lambda a: a["id"])
def test_expected_verdicts_reproduce_the_pre_registered_reached_rungs_and_scores(answer):
    result = _score_expected(answer)
    assert result.level_check["applies"] == FIXTURE["questions"][answer["questionId"]]["applies"]
    for ladder, expected in answer["ladders"].items():
        assert result.level_check["reached"][ladder] == expected["reached"], ladder
        if expected.get("evidence"):
            assert result.guarded[ladder].quote_found is True  # the expected evidence is verbatim
    assert tuple(_overalls(result).values()) == EXPECTED_OVERALLS[answer["id"]]
    assert result.answered_the_question == answer["answeredTheQuestion"]


@pytest.mark.parametrize("answer", ANSWERS, ids=lambda a: a["id"])
def test_g1_order_holds_for_every_expected_answer(answer):
    overalls = list(_overalls(_score_expected(answer)).values())
    assert overalls == sorted(overalls, reverse=True)


def test_g2_simple_a1_answers_score_high_at_a1_and_far_lower_at_b1_and_b2():
    for answer in BY_TYPE("simple_a1"):
        o = _overalls(_score_expected(answer))
        assert o["A1"] >= 8.0 and o["A1"] - o["B1"] >= 3.0 and o["A1"] - o["B2"] >= 4.5, answer["id"]


def test_g3_strong_b1_answers_use_the_top_of_the_scale():
    for answer in BY_TYPE("strong_b1"):
        o = _overalls(_score_expected(answer))
        assert o["A2"] >= 9.0 and o["B1"] >= 7.5, answer["id"]


def test_g4_typical_a2_answers_sit_at_a2():
    for answer in BY_TYPE("typical_a2"):
        o = _overalls(_score_expected(answer))
        assert o["A2"] >= 7.5 and o["B1"] <= o["A2"] - 2.0, answer["id"]


def test_g5_exam_standard_really_sits_between():
    typical = [_overalls(_score_expected(a)) for a in BY_TYPE("typical_a2")]
    strong = [_overalls(_score_expected(a)) for a in BY_TYPE("strong_b1")]
    assert sum(o["A2"] - o["exam"] >= 0.5 for o in typical) >= 4
    assert sum(o["exam"] - o["B1"] >= 0.5 for o in strong) >= 4


def test_g6_weak_answers_are_marked_down_at_exam_standard():
    for answer in BY_TYPE("weak"):
        assert _overalls(_score_expected(answer))["exam"] <= 6.5, answer["id"]


def test_g7_the_off_topic_answer_is_capped_at_every_level():
    (answer,) = BY_TYPE("control_off_topic")
    result = _score_expected(answer)
    assert all(score <= 2.0 for score in _overalls(result).values())
    assert result.answered_the_question is False


def test_the_b2_control_reaches_b2_everywhere():
    (answer,) = BY_TYPE("control_b2")
    result = _score_expected(answer)
    assert set(_overalls(result).values()) == {10.0}
    assert result.cefr_level == "B2"


def test_g12_needs_lists_for_the_simple_a1_answers():
    for answer in BY_TYPE("simple_a1"):
        result = _score_expected(answer)
        needs = result.level_check["needsByLevel"]
        assert needs["A1"] == [], answer["id"]
        assert len(needs["B1"]) >= 2, answer["id"]
        for items in needs.values():
            assert {i["ladder"] for i in items} <= set(result.level_check["applies"])


def test_g14_the_strong_hol_01_answer_reaches_b1_on_every_applicable_ladder_with_the_past_only():
    answer = next(a for a in ANSWERS if a["id"] == "hol_01_strong_b1")
    result = _score_expected(answer)
    assert result.level_check["applies"] == ["T", "L", "F", "R", "A"]
    assert all(ll.LEVELS.index(name) >= ll.LEVELS.index("B1") for name in result.level_check["reached"].values())
    assert result.level_check["needsByLevel"]["B1"] == []
    assert result.level_check["needsByLevel"]["exam"] == []


def test_the_levelcheck_does_not_depend_on_anything_but_the_verdicts_the_demands_and_the_transcript():
    """Level-blind: the same reply scored twice, in any order, gives the same result for all five levels."""
    answer = next(a for a in ANSWERS if a["id"] == "fam_01_weak")
    first = _score_expected(answer).level_check
    for _other in ANSWERS:
        _score_expected(_other)
    assert _score_expected(answer).level_check == first
