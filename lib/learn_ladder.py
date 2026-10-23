"""Learn marking-level ladder: which ladders apply, guardrails, points and the needs list.

Pure scoring code for the `ladder-v1` scheme (frontend `docs/systems/learn-marking-levels.md`,
cited as `learn-marking §N`; decision `docs/decisions/0013-learn-marking-level-ladder.md`). The
model only judges rungs (yes/no with a quote); everything here is deterministic. Nothing in this
module takes the learner's marking level as an input, because the level never reaches the backend
(ADR 0013 #2): it computes the result for all five levels, and the app picks one to show.

Imports nothing from `main.py` and does no I/O, so it is unit-testable on its own. The prompt, the
`marking` request field, the kill switch and the cache key are Batch 1's later slices; the functions
here are what they will call.

Every number below that no source states is UNVALIDATED (learn-marking §6): the points, equal
weights, the relevance cap, the cap on needs items and the 20-word example limit.

Readings the spec leaves open (change them here and in the spec together):
- The model's raw reply is ``{ladder: {"verdicts": [4 × "y"|"n"|null], "quote": str, "next": str}}``;
  Batch 1's prompt must ask for exactly these keys.
- The `quote` is evidence for the *highest* "y" only (§7). If it is missing or not in the transcript,
  that one "y" becomes "n"; the "y"s below it stay.
- A `next` example is kept only if it was written for the rung the learner needs next (the model's own
  first "n" is that rung), is at most 20 words, is not empty, and is neither the student's own words nor
  the learner sentence. Whitespace is trimmed; nothing else is ever rewritten.
- Quote matching also ignores punctuation, and matches whole words (so "personne" is not in "personnes").
- The pre-registered test set's `interpretations` (T-B1, R-B1, A-B1-B2, tense, O-justification) are
  written into the model wording below, because the expected verdicts rely on them.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

LADDER_SCHEME = "ladder-v1"

LEVELS = ("A1", "A2", "B1", "B2")  # the four rungs of every ladder, lowest first
MARKING_LEVELS = ("A1", "A2", "exam", "B1", "B2")  # the five levels a learner can pick (§5)
LADDER_ORDER = ("T", "L", "O", "F", "R", "A")  # the order of the model's reply
NEEDS_ORDER = ("T", "O", "F", "L", "A", "R")  # §6.1
SUB_SCORE_KEYS = ("comm", "fluency", "know", "acc")

# §6.1 caps. UNVALIDATED.
MAX_NEEDS_ITEMS = 3
MAX_EXAMPLE_WORDS = 20

# §6 step 2. Keyed by gap = required rung - reached rung. UNVALIDATED.
POINTS_ABOVE = 10.0  # gap <= -1
POINTS_MEETS = 8.0  # gap 0
POINTS_MEETS_AT_TOP = 10.0  # gap 0 when the requirement is B2, the top rung
POINTS_ONE_SHORT = 5.0
POINTS_TWO_SHORT = 2.5
POINTS_FAR_SHORT = 1.0  # gap >= 3

# §6 step 4: if T-A1 is not met, every sub-score is capped here (TN p.11 lowest bands). UNVALIDATED.
RELEVANCE_CAP = 2.0

# §4. Decided by code from the resolved demands, never by the model or the marking level.
_O_STRUCTURES = frozenset({"opinion", "justification", "comparison"})
_O_COGNITIVE_DEMANDS = frozenset({"justify", "compare", "hypothesize"})
_F_PAST = frozenset({"past"})
_F_FUTURE = frozenset({"future", "conditional"})

# §5: the five levels each require their own rung on every ladder, except Exam standard, which is
# "A2 with elements of B1" (TN p.11-12) and so is a per-ladder requirement.
_EXAM_REQUIRED_RUNG = {"T": 2, "L": 3, "O": 3, "F": 3, "R": 2, "A": 3}


def required_rung(ladder: str, marking_level: str) -> int:
    """The rung (1 = A1 ... 4 = B2) a ladder must reach for `marking_level`."""
    if marking_level == "exam":
        return _EXAM_REQUIRED_RUNG[ladder]
    return LEVELS.index(marking_level) + 1


# ── The ladder table (learn-marking §3) ─────────────────────────────────────────────────────
# `model` is what the judge sees, `learner` is the sentence shown in "what your answer needs",
# `source` is the citation. Rung 0 (A1) of O and F is None: those ladders have no A1 requirement,
# and a missing rung counts as met (§6 step 1). Where a model wording carries a reading from the
# pre-registered test set's `interpretations` (tests/fixtures/learn_marking_eval_v1), the id is in
# a comment. Learner wording never names a tense, opinion or reason on a ladder that does not
# invite one (§4, gate G14).

_T = {
    "label": "Developing your answer",
    "rungs": [
        {
            "model": "Gives at least one piece of information that answers the question (isolated phrases are fine).",
            "learner": "Say at least one thing that answers the question.",
            "source": "CV p.62 Overall oral production A1; TN p.11 lowest bands (relevance)",
        },
        {
            "model": "Gives a short series of relevant points; a list of simple sentences is fine.",
            "learner": "Give a few relevant points, not just one.",
            "source": "CV p.62 A2",
        },
        {
            # interpretation T-B1: a reason attached to an opinion counts under O, not here; a list of
            # one- or two-attribute facts is rung 2. Iteration 1 made "developed" checkable: the first gate
            # run counted an extra separate fact as development (7 of 44 mismatches).
            "model": (
                "The points come as a sequence, and at least one is developed: the answer says more about that "
                "same point (why or how, an example of it, what follows from it). Not development: a further "
                "separate fact or attribute, even about the same person and even joined by et or mais (rung 2); "
                "a time, number, place or name inside the fact; when O is listed, the reason for the opinion."
            ),
            "learner": "Put your points in a sequence and develop one of them with an extra detail or an example.",
            "source": "CV p.62 B1; CV p.140 Thematic development B1; 'at least one' UNVALIDATED (TN p.11 Good band)",
        },
        {
            "model": "Main points are expanded and supported with sub-points and relevant examples.",
            "learner": "Expand your main points with sub-points and examples.",
            "source": "CV p.62 B2; CV p.140 B2",
        },
    ],
}

_L = {
    "label": "Linking your ideas",
    "rungs": [
        {
            # "et" joining two nouns was judged "n" in the first gate run (G12), and iteration 1's "(any et
            # joining two words counts)" was then read as excluding et between two clauses (G3).
            "model": (
                "Links words or ideas with very basic linear connectors such as et, puis: any et or puis "
                "counts, whether it joins two words, two phrases or two clauses."
            ),
            "learner": "Link your ideas with simple words like et or puis.",
            "source": "CV p.141 Coherence and cohesion A1",
        },
        {
            "model": "Uses simple connectors such as mais, parce que, car (or a more advanced one: donc, cependant…).",
            "learner": "Use simple connectors like mais, parce que or car.",
            "source": "CV p.141 A2",
        },
        {
            "model": (
                "Joins several short points into one connected sequence, linking across sentences "
                "(d'abord, ensuite, après, donc, quand), not only inside one sentence."
            ),
            "learner": "Join your sentences into one connected sequence with words like d'abord, ensuite, après, donc or quand.",
            "source": "CV p.141 B1; Syl p.13; French examples from Syl p.26 UNVALIDATED as a list",
        },
        {
            "model": (
                "Uses a range of linking expressions showing contrast, consequence or concession "
                "(cependant, par contre, alors que, même si, c'est pourquoi)."
            ),
            "learner": "Use a range of linking phrases for contrast and consequence, like cependant, par contre, même si or c'est pourquoi.",
            "source": "CV p.141 B2 and B2+",
        },
    ],
}

_O = {
    "label": "Opinions and reasons",
    "rungs": [
        None,  # CV p.64 gives no A1 descriptors
        {
            "model": "Gives an opinion or preference in simple terms (j'aime, je préfère, je pense que, c'est…).",
            # interpretation O-justification: on a question tagged `justification` a simple reason is also needed.
            "modelWhy": (
                "Gives an opinion or preference in simple terms (j'aime, je préfère, je pense que, c'est…) "
                "and a simple reason for it (as defined in rung 3), because the question asks why."
            ),
            "learner": "Say what you think or prefer, in simple terms (j'aime…, je pense que…).",
            "learnerWhy": "Say what you think or prefer and give a simple reason (j'aime… parce que…).",
            "source": "CV p.64 A2 and A2+",
        },
        {
            "model": (
                "Gives at least one reason or explanation for an opinion, plan or action, presented as why "
                "(parce que, car, comme, donc…); a liking, habit or fact merely placed beside it is not a reason."
            ),
            "learner": "Give at least one reason for your opinion.",
            "source": "CV p.64 B1; Syl p.13; TN p.11 Good band",
        },
        {
            "model": "Develops a viewpoint: a chain of reasons, or advantages and disadvantages.",
            "learner": "Develop your view with a chain of reasons, or the advantages and disadvantages.",
            "source": "CV p.64 B2",
        },
    ],
}

# F is checked only for the parts the question asks (§3.4): `past` and/or `future` (which covers
# future and conditional, either verb form accepted). A past-only question is never marked down for
# leaving out the future, and the other way round.
_F = {
    "label": {
        "past": "Talking about the past",
        "future": "Talking about plans and wishes",
        "both": "Talking about the past and the future",
    },
    "parts": {
        "past": [
            None,
            {
                "model": (
                    "Talks about the past event or experience the question asks about, in a past form "
                    "(a wrongly formed verb is an Accuracy matter, not a failure here)."
                ),
                "learner": "Tell it in the past (je suis allé(e)…, j'ai visité…).",
                "source": "CV p.62-63 A2",
            },
            {
                "model": "Gives a detailed account of the experience that also says how they felt or reacted.",
                "learner": "Say how you felt or reacted to what happened.",
                "source": "CV p.62-63 B1; Syl p.13",
            },
            {
                "model": "Explains the personal significance of the experience in detail.",
                "learner": "Explain what the experience meant to you.",
                "source": "CV p.62 B2",
            },
        ],
        "future": [
            None,
            {
                "model": "Talks about the plan or wish the question asks about, in a future or conditional form.",
                "learner": "Talk about your plan or wish with the future or conditional (je vais…, je voudrais…, j'aimerais…).",
                "source": "CV p.62-63 A2",
            },
            {
                "model": (
                    "Describes hopes, ambitions or the imagined situation in some detail: what it would be like "
                    "or what they would do, beyond naming the wish; facts about their present life do not count."
                ),
                "learner": "Describe your hopes or the imagined situation in more detail.",
                "source": "CV p.62-63 B1; Syl p.13",
            },
            {
                "model": "Speculates about causes or consequences.",
                "learner": "Speculate about what could happen and why.",
                "source": "CV p.174 B2; UNCERTAIN: no scale covers hypotheticals below B2",
            },
        ],
    },
}

_R = {
    "label": "Range of language",
    "rungs": [
        {
            "model": "Uses simple expressions and one-clause sentences on the topic.",
            "learner": "Use simple expressions and short sentences on the topic.",
            "source": "CV p.130 General linguistic range A1",
        },
        {
            "model": "Uses complete basic sentence patterns with everyday topic vocabulary.",
            "learner": "Use complete basic sentences with everyday words for the topic.",
            "source": "CV p.130-131 A2; Vocabulary range A2",
        },
        {
            # interpretation R-B1: 'more than one clause' means a subordinate clause; et/mais are L's job;
            # one formulaic 'je pense que ... parce que' sentence is not "some sentences". Iteration 1 states
            # the plural as a count: at least two sentences.
            "model": (
                "Uses varied topic vocabulary and some sentences with more than one clause, meaning a "
                "subordinate clause (qui, que, où, quand, si, parce que): at least two different sentences "
                "with one. Clauses joined only by et or mais do not count."
            ),
            "learner": "Add some sentences with a second clause (qui, que, quand, si, parce que) and use a wider range of topic words.",
            "source": "CV p.130-131 B1; Vocabulary range B1; the more-than-one-clause test is UNVALIDATED",
        },
        {
            "model": (
                "Uses some complex sentence forms and varied wording (e.g. si + imperfect/conditional, dont, "
                "ce que, en + present participle, pluperfect: any complex form counts, none is required)."
            ),
            "learner": "Try some complex sentence forms (for example with si, dont, ce que, or a verb ending in -ant after en) and vary your wording.",
            "source": "CV p.130-131 B2; Vocabulary range B2; Syl p.25-26",
        },
    ],
}

_A = {
    "label": "Accuracy",
    "rungs": [
        {
            "model": "Some simple structures are correct (je suis, j'ai, il y a).",
            "learner": "Get a few simple structures right (je suis, j'ai, il y a).",
            "source": "CV p.132 Grammatical accuracy A1",
        },
        {
            "model": "Despite basic mistakes, it is usually clear what the student means.",
            "learner": "Make sure your meaning stays clear, even when you make small mistakes.",
            "source": "CV p.132 A2",
        },
        {
            # interpretations A-B1-B2 and tense: up to two basic errors still meets B1; three or four do not.
            # Both rungs follow the count of the errors the feedback call found, which the checklist is given
            # (main.ladder_check_errors): in the first gate run the model listed every seeded error but still
            # judged two of them "occasional slips" (A-B2, 5 of 44).
            "model": (
                "Everyday patterns are mostly correct (common verbs, common agreements, the tense the question "
                "asks for); errors are mainly in more ambitious structures. Count the errors under ERRORS ALREADY "
                "FOUND: 0-2 is \"y\", 3 or more is \"n\", leaving out any whose only problem is a spelling that "
                "sounds the same as the correct form (the transcript comes from speech recognition)."
            ),
            "learner": "Get everyday patterns right: common verbs, agreements and the tense the question asks for.",
            "source": "CV p.132 B1 and intro; Syl p.13 'reasonable accuracy'; TN p.12",
        },
        {
            "model": (
                "No mistake causes misunderstanding; slips are occasional. Counted the same way: 0-1 error that "
                "does not confuse the meaning is \"y\", 2 or more is \"n\"."
            ),
            "learner": "Avoid any mistake that could confuse the listener; only an occasional slip.",
            "source": "CV p.132 B2",
        },
    ],
}

LADDER_TABLE: dict[str, dict[str, Any]] = {"T": _T, "L": _L, "O": _O, "F": _F, "R": _R, "A": _A}


def _table_fingerprint() -> str:
    """Everything that decides a ladder result: the wording the judge and the learner see, the
    requirements, the points and the caps. Changing any of it changes the version."""
    payload = {
        "table": LADDER_TABLE,
        "exam": _EXAM_REQUIRED_RUNG,
        "points": [
            POINTS_ABOVE, POINTS_MEETS, POINTS_MEETS_AT_TOP, POINTS_ONE_SHORT, POINTS_TWO_SHORT, POINTS_FAR_SHORT,
        ],
        "cap": RELEVANCE_CAP,
        "needs": [MAX_NEEDS_ITEMS, MAX_EXAMPLE_WORDS, list(NEEDS_ORDER)],
        "applies": [sorted(_O_STRUCTURES), sorted(_O_COGNITIVE_DEMANDS), sorted(_F_PAST), sorted(_F_FUTURE)],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# Recorded in every levelCheck. Pinned in tests/test_learn_ladder.py: bump it by updating the pin
# there, deliberately, whenever the wording, requirements, points or caps change.
LEARN_LADDER_VERSION = f"learn-ladder-v1-{_table_fingerprint()[:12]}"


# ── Which ladders apply (learn-marking §4) ──────────────────────────────────────────────────

def _as_set(demands: Mapping[str, Any] | None, key: str) -> set[str]:
    return set((demands or {}).get(key) or [])


def f_parts(demands: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The parts of ladder F the question asks for: 'past' and/or 'future'."""
    frames = _as_set(demands, "timeFrames")
    parts = []
    if frames & _F_PAST:
        parts.append("past")
    if frames & _F_FUTURE:
        parts.append("future")
    return tuple(parts)


def invites_opinion(demands: Mapping[str, Any] | None) -> bool:
    if not demands:
        return False
    return bool(_as_set(demands, "structures") & _O_STRUCTURES) or demands.get("cognitiveDemand") in _O_COGNITIVE_DEMANDS


def asks_why(demands: Mapping[str, Any] | None) -> bool:
    """The question explicitly asks for a reason (a `justification` tag), so O-A2 needs one too."""
    return "justification" in _as_set(demands, "structures")


def applicable_ladders(demands: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The ladders that apply to a question, in LADDER_ORDER.

    T, L, R and A always apply. O applies when the question invites an opinion or reason, F when it
    asks about the past, future or an imagined situation. Unresolved demands (None: a follow-up, or a
    demandsVersion mismatch) give T, L, R, A only: never fail a learner on something the question may
    not ask. A plain `explain` does not invite O.
    """
    ladders = ["T", "L"]
    if invites_opinion(demands):
        ladders.append("O")
    if f_parts(demands):
        ladders.append("F")
    ladders += ["R", "A"]
    return tuple(ladders)


# ── Wording ─────────────────────────────────────────────────────────────────────────────────

def ladder_label(ladder: str, demands: Mapping[str, Any] | None = None) -> str:
    """The learner-facing name of a ladder, for the needs list."""
    if ladder != "F":
        return LADDER_TABLE[ladder]["label"]
    parts = f_parts(demands)
    return LADDER_TABLE["F"]["label"]["both" if len(parts) == 2 else (parts[0] if parts else "past")]


def rung_text(ladder: str, rung_index: int, demands: Mapping[str, Any] | None, audience: str) -> str | None:
    """The wording of one rung (0 = A1 ... 3 = B2) for the `model` or the `learner`.

    None for the rung that has no requirement (the A1 rung of O and F). Chosen from the question's
    demands: F carries only the parts the question asks for, and O-A2 asks for a reason only when the
    question asks why.
    """
    if audience not in ("model", "learner"):
        raise ValueError(f"audience must be 'model' or 'learner', got {audience!r}")
    if ladder == "F":
        parts = f_parts(demands) or ("past",)
        texts = []
        for part in parts:
            rung = LADDER_TABLE["F"]["parts"][part][rung_index]
            if rung is None:
                return None
            texts.append(rung[audience])
        if len(texts) == 1:
            return texts[0]
        if audience == "learner":
            return " ".join(texts)
        return "Both parts must be met. Past part: " + texts[0] + " Future or imagined part: " + texts[1]
    rung = LADDER_TABLE[ladder]["rungs"][rung_index]
    if rung is None:
        return None
    if ladder == "O" and rung_index == 1 and asks_why(demands):
        return rung["modelWhy" if audience == "model" else "learnerWhy"]
    return rung[audience]


# ── Quote matching (learn-marking §6 step 1) ────────────────────────────────────────────────

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "´": "'", "`": "'", "′": "'"})
_FRAGMENT_SPLIT = re.compile(r"…|\.{3,}")


def _normalise(text: str) -> str:
    """Case, accents, apostrophe style, punctuation and spacing are ignored; word boundaries are kept."""
    text = unicodedata.normalize("NFKD", text.translate(_APOSTROPHES))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = text.replace("œ", "oe").replace("æ", "ae")
    text = re.sub(r"[^\w']+|_", " ", text)
    return " ".join(token.strip("'") for token in text.split() if token.strip("'"))


def quote_in_transcript(quote: str | None, transcript: str) -> bool:
    """True when every '…'-separated fragment of `quote` occurs in `transcript` as whole words.

    An empty quote (or one that is only separators) is not found: a "yes" needs evidence.
    """
    if not isinstance(quote, str):
        return False
    fragments = [_normalise(part) for part in _FRAGMENT_SPLIT.split(quote)]
    fragments = [part for part in fragments if part]
    if not fragments:
        return False
    haystack = f" {_normalise(transcript)} "
    return all(f" {part} " in haystack for part in fragments)


# ── Guardrails (learn-marking §6 steps 1 and 4) ─────────────────────────────────────────────

class LadderValidationError(ValueError):
    """The model's levelCheck is missing or malformed. The provider call raises it, so the existing
    Groq -> Gemini fallback runs."""


def _no_a1_requirement(ladder: str) -> bool:
    return ladder in ("O", "F")


def _verdict(value: Any) -> Any:
    """A verdict as the model meant it: the string "null" is read as JSON null.

    A contract fix, not a scoring rule: the first gate run (2026-10-19) lost a whole reply because
    the model wrote "null" in quotes for the A1 rung of O and F. A null then follows the usual rules
    (met on the A1 rung of O and F, "no" anywhere else).
    """
    if isinstance(value, str) and value.strip().lower() == "null":
        return None
    return value


def validate_raw_levelcheck(raw: Any, applies: Sequence[str]) -> None:
    """Raises LadderValidationError unless every applicable ladder has four verdicts, each "y", "n" or null.

    The shape (the Batch 1 prompt asks for it): ``{"T": {"verdicts": ["y","y","n","n"], "quote": "…",
    "next": "…"}, …}``. A ladder that does not apply is ignored if present. A null where only O and F
    allow one is not malformed; it counts as "no". The string "null" is read as null (`_verdict`).
    """
    if not isinstance(raw, Mapping):
        raise LadderValidationError("levelCheck is not an object")
    for ladder in applies:
        entry = raw.get(ladder)
        if not isinstance(entry, Mapping):
            raise LadderValidationError(f"levelCheck is missing ladder {ladder}")
        verdicts = entry.get("verdicts")
        if not isinstance(verdicts, (list, tuple)) or len(verdicts) != 4:
            raise LadderValidationError(f"ladder {ladder} needs four verdicts")
        if any(_verdict(v) not in ("y", "n", None) for v in verdicts):
            raise LadderValidationError(f"ladder {ladder} has a verdict that is not y, n or null")


def reached_rung(verdicts: Sequence[str | None]) -> int:
    """How high a ladder climbs: the highest rung (0 = none ... 4 = B2) up to which every rung is met.

    A null (the A1 rung of O and F) counts as met; a "yes" above a "no" is ignored.
    """
    rung = 0
    for verdict in verdicts:
        if verdict == "n":
            break
        rung += 1
    return rung


@dataclass(frozen=True)
class GuardedLadder:
    ladder: str
    verdicts: tuple[str | None, ...]  # after the guardrails; "y" above a "n" is kept here but ignored by `reached`
    reached: int
    quote_found: bool | None  # None when there was no "yes" to evidence
    example: str | None  # the model's `next` example, only when it passed the drop-only filter


def _clean_example(
    example: Any, transcript: str, learner_sentence: str | None, example_rung: int, reached: int
) -> str | None:
    """Drop-only (§6.1): an over-long, empty or copied example is dropped, never rewritten.

    Also dropped when it was written for a rung other than the one the learner needs next (the quote
    guardrail moved the first "no").
    """
    if example_rung != reached or not isinstance(example, str):
        return None
    example = example.strip()
    if not example or len(re.findall(r"\w+", example)) > MAX_EXAMPLE_WORDS:
        return None
    flat = _normalise(example)
    if not flat or flat in _normalise(transcript):
        return None  # the student's own words back, so nothing to learn from it
    if learner_sentence and flat == _normalise(learner_sentence):
        return None
    return example


def apply_guardrails(
    raw: Mapping[str, Any], applies: Sequence[str], transcript: str, demands: Mapping[str, Any] | None = None
) -> dict[str, GuardedLadder]:
    """Turn the model's raw verdicts into guarded ones, per applicable ladder.

    1. The A1 rung of O and F has no requirement: whatever the model wrote there, it counts as met.
    2. Any other null counts as "no".
    3. The quote is evidence for the highest "yes". If it is not in the transcript (or missing), that
       "yes" becomes a "no".
    4. `reached` ignores a "yes" above a "no".

    Raises LadderValidationError for a missing or malformed ladder. Level-blind by construction.
    """
    validate_raw_levelcheck(raw, applies)
    guarded: dict[str, GuardedLadder] = {}
    for ladder in applies:
        entry = raw[ladder]
        verdicts: list[str | None] = []
        for index, verdict in enumerate(entry["verdicts"]):
            if index == 0 and _no_a1_requirement(ladder):
                verdicts.append(None)
            else:
                verdicts.append("y" if _verdict(verdict) == "y" else "n")
        # Where the model's own first "no" sits, before the quote check can move it.
        model_first_no = next((i for i, v in enumerate(verdicts) if v == "n"), 4)
        yes_rungs = [i for i, v in enumerate(verdicts) if v == "y"]
        quote_found: bool | None = None
        if yes_rungs:
            quote_found = quote_in_transcript(entry.get("quote"), transcript)
            if not quote_found:
                verdicts[yes_rungs[-1]] = "n"
        reached = reached_rung(verdicts)
        next_rung = rung_text(ladder, reached, demands, "learner") if reached < 4 else None
        guarded[ladder] = GuardedLadder(
            ladder=ladder,
            verdicts=tuple(verdicts),
            reached=reached,
            quote_found=quote_found,
            example=_clean_example(entry.get("next"), transcript, next_rung, model_first_no, reached),
        )
    return guarded


# ── Points and sub-scores (learn-marking §6 steps 2-5) ──────────────────────────────────────

def ladder_points(reached: int, required: int) -> float:
    """Points for one ladder at one level, from gap = required rung - reached rung."""
    gap = required - reached
    if gap <= -1:
        return POINTS_ABOVE
    if gap == 0:
        return POINTS_MEETS_AT_TOP if required == len(LEVELS) else POINTS_MEETS
    if gap == 1:
        return POINTS_ONE_SHORT
    if gap == 2:
        return POINTS_TWO_SHORT
    return POINTS_FAR_SHORT


def sub_scores_all_levels(reached: Mapping[str, int], applies: Sequence[str]) -> dict[str, dict[str, float]]:
    """The four sub-scores at each of the five marking levels, unrounded.

    comm = mean of the applicable T/O/F points; fluency = L; know = R; acc = A. If T-A1 is not met
    (reached["T"] == 0), every sub-score is capped at RELEVANCE_CAP. Neither the cap nor the set of
    ladders depends on the level, which is what makes the order A1 >= A2 >= Exam >= B1 >= B2 hold.
    """
    capped = reached["T"] < 1
    result: dict[str, dict[str, float]] = {}
    for level in MARKING_LEVELS:
        points = {ladder: ladder_points(reached[ladder], required_rung(ladder, level)) for ladder in applies}
        comm_points = [points[ladder] for ladder in ("T", "O", "F") if ladder in points]
        scores = {
            "comm": sum(comm_points) / len(comm_points),
            "fluency": points["L"],
            "know": points["R"],
            "acc": points["A"],
        }
        if capped:
            scores = {key: min(value, RELEVANCE_CAP) for key, value in scores.items()}
        result[level] = scores
    return result


def overall_from_sub_scores(sub_scores: Mapping[str, float]) -> float:
    """The client's `computeOverall` (src/domain/scoring.ts): the mean, rounded half up to one decimal.

    One formula on both sides, so the backend never rounds sub-scores itself.
    """
    values = [sub_scores[key] for key in SUB_SCORE_KEYS]
    return math.floor(sum(values) / len(values) * 10 + 0.5) / 10


def derive_cefr_level(sub_scores_by_level: Mapping[str, Mapping[str, float]]) -> str:
    """The highest of A1-B2 whose overall is >= 8, or A1 if none. "exam" is never a cefrLevel."""
    best = "A1"
    for level in LEVELS:
        if overall_from_sub_scores(sub_scores_by_level[level]) >= 8:
            best = level
    return best


def reached_name(rung: int) -> str:
    return "none" if rung == 0 else LEVELS[rung - 1]


# ── "What your answer needs for <level>" (learn-marking §6.1) ───────────────────────────────

def needs_by_level(
    guarded: Mapping[str, GuardedLadder], applies: Sequence[str], demands: Mapping[str, Any] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Per marking level, the applicable ladders below that level's requirement, T O F L A R, at most 3.

    Each item: the next rung's learner sentence, `firstStep` when the answer is two or more rungs
    short, and the model's example (None when it was dropped). Empty when the answer meets the level.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for level in MARKING_LEVELS:
        items = []
        for ladder in NEEDS_ORDER:
            if ladder not in applies:
                continue
            ladder_result = guarded[ladder]
            gap = required_rung(ladder, level) - ladder_result.reached
            if gap <= 0:
                continue
            items.append(
                {
                    "ladder": ladder,
                    "label": ladder_label(ladder, demands),
                    "nextRung": LEVELS[ladder_result.reached],
                    "text": rung_text(ladder, ladder_result.reached, demands, "learner"),
                    "firstStep": gap >= 2,
                    "example": ladder_result.example,
                }
            )
        result[level] = items[:MAX_NEEDS_ITEMS]
    return result


# ── The whole thing ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LadderResult:
    """What the backend adds to a ladder-scheme reply (learn-marking §7)."""

    level_check: dict[str, Any]  # {scheme, ladderVersion, applies, reached, subScoresByLevel, needsByLevel}
    answered_the_question: bool  # the T-A1 verdict, after the guardrails
    cefr_level: str
    guarded: dict[str, GuardedLadder] = field(repr=False)


def score_ladders(raw: Any, demands: Mapping[str, Any] | None, transcript: str) -> LadderResult:
    """Verdicts in, every score out. Takes no marking level: the level never reaches the backend.

    `fb.scores` is the Exam-standard entry of `levelCheck["subScoresByLevel"]` (learn-marking §8.1);
    mapping it onto the reply's keys is the caller's job. Raises LadderValidationError if the model's
    levelCheck is unusable.
    """
    applies = applicable_ladders(demands)
    guarded = apply_guardrails(raw, applies, transcript, demands)
    reached = {ladder: guarded[ladder].reached for ladder in applies}
    sub_scores = sub_scores_all_levels(reached, applies)
    level_check = {
        "scheme": LADDER_SCHEME,
        "ladderVersion": LEARN_LADDER_VERSION,
        "applies": list(applies),
        "reached": {ladder: reached_name(reached[ladder]) for ladder in applies},
        "subScoresByLevel": sub_scores,
        "needsByLevel": needs_by_level(guarded, applies, demands),
    }
    return LadderResult(
        level_check=level_check,
        answered_the_question=reached["T"] >= 1,
        cefr_level=derive_cefr_level(sub_scores),
        guarded=guarded,
    )
