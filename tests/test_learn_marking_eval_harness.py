"""The Learn marking gate-run harness and its report (scripts/learn_marking_eval.py).

No model is called: a stub provider stands in. The main check is the oracle run — a provider that
answers with exactly the pre-registered expected verdicts, evidence and seeded errors must pass every
automatic gate, which proves the report computes the gates the way the protocol defines them (and
mirrors the Stage 0b dry run in test_learn_ladder.py). The rest pins the protocol's harness rules:
the production request, resume, the Stage 1 stop rules, the daily-limit stop and retry-after.

Run: pytest tests/test_learn_marking_eval_harness.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import learn_marking_eval as harness  # noqa: E402
import main  # noqa: E402

FIXTURE = harness.load_fixture()
CLIENT_INPUTS = harness.load_client_inputs()
ANSWERS = {a["id"]: a for a in FIXTURE["answers"]}


def oracle_reply(answer: dict) -> dict:
    """What a perfect judge would return, both calls' keys in one dict: the expected verdicts, each ladder's
    evidence for its highest "y" (the checklist's), and the seeded errors reported as grammar items (the
    feedback call's). oracle_provider splits it by call."""
    level_check = {}
    for ladder, expected in answer["ladders"].items():
        verdicts = expected["verdicts"]
        has_yes = "y" in verdicts
        level_check[ladder] = {"verdicts": verdicts, "quote": expected["evidence"] if has_yes else "", "next": ""}
    errors = [
        {"id": f"e{n}", "msg": f"« {e['quote']} » is wrong.", "correction": e["correction"], "quote": e["quote"], "severity": "major"}
        for n, e in enumerate(answer["seededErrors"])
    ]
    return {
        "levelCheck": level_check,
        "demandCheck": {"met": answer["demandCheck"]["met"], "quote": ""},
        "grammar": {"critical": errors, "polish": []},
        "best_moment": "",
    }


CHECK_KEYS = ("levelCheck", "demandCheck")


def answer_in(prompt: str) -> str:
    return next(i for i, a in ANSWERS.items() if f"STUDENT TRANSCRIPT (French): {main.clean_transcript(a['transcript'])}\n" in prompt)


def oracle_provider(calls: list, *, finish_reason="stop", tokens=3500, mutate=None):
    """Both production calls: the feedback call gets the grammar half of oracle_reply, the level
    checklist (kind "check") its levelCheck/demandCheck. `calls` records the feedback calls only, as
    (answerId, depth, prompt). `mutate` sees the whole reply; a string reply is sent as is."""
    async def call(prompt: str, depth: str, kind: str = "feedback") -> harness.ProviderReply:
        answer_id = answer_in(prompt)
        if kind == "feedback":
            calls.append((answer_id, depth, prompt))
        reply = oracle_reply(ANSWERS[answer_id])
        if mutate:
            reply = mutate(answer_id, reply)
        if isinstance(reply, dict):
            reply = {k: v for k, v in reply.items() if (k in CHECK_KEYS) == (kind == "check")}
        content = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return harness.ProviderReply(
            content=content,
            usage={"promptTokens": tokens - 1500, "completionTokens": 1500, "totalTokens": tokens},
            finish_reason=finish_reason,
            model="groq/stub",
        )

    return call


async def _no_sleep(seconds):
    _no_sleep.waits.append(seconds)


_no_sleep.waits = []


def run(stage, log, provider, **kw):
    return asyncio.run(harness.run_stage(stage, log, providers={"groq": provider, "gemini": provider}, sleep=_no_sleep, check=False, **kw))


@pytest.fixture(autouse=True)
def _ladder_on(monkeypatch):
    monkeypatch.delenv("LEARN_LADDER_ENABLED", raising=False)
    _no_sleep.waits = []


# ── Inputs ──────────────────────────────────────────────────────────────────


def test_client_inputs_cover_every_answer_and_match_its_text():
    # Built by the frontend's `npm run learn:marking-inputs` from the real client code.
    assert set(CLIENT_INPUTS["answers"]) == set(ANSWERS)
    assert CLIENT_INPUTS["demandsVersion"] == main.LEARN_DEMANDS_VERSION
    for answer_id, entry in CLIENT_INPUTS["answers"].items():
        assert entry["transcriptSha256"] == hashlib.sha256(ANSWERS[answer_id]["transcript"].encode()).hexdigest()
        assert entry["depth"] in ("brief", "standard", "deep")
        assert entry["demandSignals"]["cognitiveDemand"] == main.LEARN_DEMANDS_BY_QUESTION_ID[entry["questionId"]]["cognitiveDemand"]


def test_the_request_is_the_one_a_learn_client_sends():
    answer = ANSWERS["hol_01_weak"]
    req = harness.build_request(answer, FIXTURE, CLIENT_INPUTS)
    entry = CLIENT_INPUTS["answers"]["hol_01_weak"]
    assert req.marking == "ladder-v1"
    assert req.question_id == "hol_01" and req.demands_version == main.LEARN_DEMANDS_VERSION
    assert main.resolve_learn_demands(req.question_id, req.demands_version) is not None
    assert req.depth == entry["depth"]
    assert req.demand_signals.model_dump(exclude_none=True) == entry["demandSignals"]
    assert req.difficulty_context is None and req.skill_context is None
    # The feedback call is v6's; the checklist carries the ladders.
    assert main.build_user_prompt(req) == main.build_user_prompt(req.model_copy(update={"marking": None}))
    assert "LEVEL CHECK — ladders for this question: T, L, F, R, A." in main.build_ladder_check_prompt(req, {})


def test_a_changed_transcript_or_unresolved_demands_refuses_to_run(monkeypatch):
    changed = dict(ANSWERS["fam_01_weak"], transcript="Autre chose.")
    with pytest.raises(SystemExit, match="transcript changed"):
        harness.build_request(changed, FIXTURE, CLIENT_INPUTS)
    stale = dict(CLIENT_INPUTS, demandsVersion="stale")
    with pytest.raises(SystemExit, match="Demands do not resolve"):
        harness.build_request(ANSWERS["fam_01_weak"], FIXTURE, stale)


def test_preconditions_refuse_an_unsigned_or_uncommitted_test_set_and_the_kill_switch(monkeypatch):
    clean = {"fixtureCommit": "abc", "fixtureContentSha256": "x", "fixtureDirty": False}
    harness.check_preconditions(FIXTURE, CLIENT_INPUTS, clean)
    with pytest.raises(SystemExit, match="not signed off"):
        harness.check_preconditions(dict(FIXTURE, status="draft"), CLIENT_INPUTS, clean)
    with pytest.raises(SystemExit, match="uncommitted"):
        harness.check_preconditions(FIXTURE, CLIENT_INPUTS, dict(clean, fixtureDirty=True))
    with pytest.raises(SystemExit, match="different demands corpus"):
        harness.check_preconditions(FIXTURE, dict(CLIENT_INPUTS, demandsVersion="old"), clean)
    monkeypatch.setenv("LEARN_LADDER_ENABLED", "0")
    with pytest.raises(SystemExit, match="LEARN_LADDER_ENABLED"):
        harness.check_preconditions(FIXTURE, CLIENT_INPUTS, clean)


def test_the_provider_timeout_is_the_protocols(monkeypatch):
    seen = []
    real_wait_for = asyncio.wait_for

    async def wait_for(awaitable, timeout):
        seen.append(timeout)
        return await real_wait_for(awaitable, timeout)

    async def create(**kwargs):
        return SimpleNamespace(model="m", usage=None, choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}"))])

    monkeypatch.setattr(main, "get_groq", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(harness.asyncio, "wait_for", wait_for)
    asyncio.run(harness.call_groq("p", "standard"))
    assert seen == [120.0]


def test_the_groq_call_is_the_production_call(monkeypatch):
    sent = []

    async def create(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(
            model="openai/gpt-oss-120b",
            usage=SimpleNamespace(prompt_tokens=3000, completion_tokens=1200, total_tokens=4200),
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"scores": {}}'))],
        )

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(main, "get_groq", lambda: fake)
    reply = asyncio.run(harness.call_groq("the prompt", "brief"))
    asyncio.run(main._call_groq("the prompt", "brief"))
    assert sent[0] == sent[1] == main._groq_feedback_kwargs("the prompt", "brief")
    assert sent[0]["messages"][0]["content"] == main.SYSTEM_PROMPT
    asyncio.run(harness.call_groq("the check", "brief", "check"))
    asyncio.run(main._call_groq_ladder_check("the check"))
    assert sent[2] == sent[3] == main._groq_ladder_check_kwargs("the check")
    assert reply.usage == {"promptTokens": 3000, "completionTokens": 1200, "totalTokens": 4200}
    assert reply.finish_reason == "stop" and reply.model == "groq/openai/gpt-oss-120b"


def test_a_daily_limit_429_from_groq_is_a_clean_stop(monkeypatch):
    async def create(**kwargs):
        raise RuntimeError("Rate limit reached ... on tokens per day (TPD): Limit 200000, Used 199000")

    monkeypatch.setattr(main, "get_groq", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    with pytest.raises(harness.DailyLimitReached):
        asyncio.run(harness.call_groq("p", "standard"))


# ── Running ─────────────────────────────────────────────────────────────────


def test_each_record_carries_what_the_protocol_asks(tmp_path):
    log = tmp_path / "log.jsonl"
    calls: list = []
    assert run("smoke", log, oracle_provider(calls)) == 0
    records = harness.read_log(log)
    assert [r["answerId"] for r in records] == FIXTURE["stages"]["smoke"]["answers"]
    for record in records:
        assert record["status"] == "ok"
        for key in ("rawOutput", "rawCheckOutput", "calls", "usage", "finishReason", "latencyMs", "model", "versions",
                    "fixtureCommit", "fixtureContentSha256", "clientInputsSha256", "request", "rawLevelCheck", "result"):
            assert key in record, key
        assert record["versions"]["learnPrompt"] == "learn-prompt-v6+ladder-check-v2"
        assert [c["kind"] for c in record["calls"]] == ["feedback", "check"]
        assert record["usage"]["totalTokens"] == sum(c["usage"]["totalTokens"] for c in record["calls"])
        # The checklist was built from this answer's feedback reply.
        req = harness.build_request(ANSWERS[record["answerId"]], FIXTURE, CLIENT_INPUTS)
        check_prompt = main.build_ladder_check_prompt(req, json.loads(record["rawOutput"]))
        assert record["versions"]["checkPromptSha256"] == hashlib.sha256(check_prompt.encode()).hexdigest()
        assert record["request"]["marking"] == "ladder-v1"
        assert record["result"]["levelCheck"]["scheme"] == "ladder-v1"
    # Depth sent is the client's.
    assert [d for _, d, _ in calls] == [CLIENT_INPUTS["answers"][i]["depth"] for i in FIXTURE["stages"]["smoke"]["answers"]]


def test_a_run_resumes_and_never_repeats_an_observation(tmp_path):
    log = tmp_path / "log.jsonl"
    calls: list = []
    budget = {"left": 5}

    async def limited(prompt, depth, kind="feedback"):
        if budget["left"] == 0:
            raise harness.DailyLimitReached("tokens per day")
        budget["left"] -= 1
        return await oracle_provider(calls)(prompt, depth, kind)

    assert run("full", log, limited) == harness.EXIT_DAILY_LIMIT
    assert len(harness.read_log(log)) == 2  # two calls an answer; the 6th call is the 3rd answer's checklist
    budget["left"] = 100
    assert run("full", log, limited) == 0
    ids = [r["answerId"] for r in harness.read_log(log)]
    assert len(ids) == 22 and len(set(ids)) == 22


def test_retry_after_is_honoured_and_a_long_one_stops_like_the_daily_limit(tmp_path):
    log = tmp_path / "log.jsonl"
    state = {"n": 0}
    oracle = oracle_provider([])

    async def flaky(prompt, depth, kind="feedback"):
        state["n"] += 1
        if state["n"] == 1:
            raise harness.TransientProviderError("429", retry_after=7.0)
        return await oracle(prompt, depth, kind)

    assert run("smoke", log, flaky) == 0
    assert _no_sleep.waits == [7.0]

    async def very_long(prompt, depth, kind="feedback"):
        raise harness.TransientProviderError("429", retry_after=3600.0)

    assert run("full", tmp_path / "other.jsonl", very_long) == harness.EXIT_DAILY_LIMIT


def test_a_rejected_request_is_recorded_and_the_run_goes_on(tmp_path):
    # Groq's 413 (prompt + max_tokens above the per-minute token limit) refuses the same request every
    # time: record it for that answer, never retry it in the run, and call the next answer.
    log = tmp_path / "log.jsonl"
    first = FIXTURE["stages"]["smoke"]["answers"][0]
    oracle = oracle_provider([])
    tries: list = []

    async def too_large_once(prompt, depth, kind="feedback"):
        if answer_in(prompt) == first and kind == "check":
            tries.append(first)
            raise harness.RequestRejected("413 Request too large")
        return await oracle(prompt, depth, kind)

    assert run("smoke", log, too_large_once) == 0
    records = harness.read_log(log)
    assert [r["status"] for r in records] == ["transport_error", "ok", "ok"]
    assert records[0]["error"].startswith("request_rejected (check):") and tries == [first]
    assert [c["kind"] for c in records[0]["calls"]] == ["feedback"]  # the feedback call's tokens are kept


def test_a_transport_failure_is_recorded_and_retried_on_the_next_run(tmp_path):
    log = tmp_path / "log.jsonl"

    async def down(prompt, depth, kind="feedback"):
        raise harness.TransientProviderError("503")

    assert run("smoke", log, down) == 0
    assert {r["status"] for r in harness.read_log(log)} == {"transport_error"}
    assert run("smoke", log, oracle_provider([])) == 0
    assert [r["status"] for r in harness.read_log(log)][-3:] == ["ok", "ok", "ok"]


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"finish_reason": "length"}, "truncated"),
        ({"tokens": 8200}, "8200 tokens"),
        ({"mutate": lambda i, r: "not json"}, "invalid JSON"),
    ],
)
def test_stage_1_stops_on_the_protocols_three_rules(tmp_path, capsys, kwargs, reason):
    log = tmp_path / "log.jsonl"
    assert run("smoke", log, oracle_provider([], **kwargs)) == harness.EXIT_STOPPED
    assert len(harness.read_log(log)) == 1
    assert reason in capsys.readouterr().out


def test_an_unusable_level_check_is_a_contract_error_and_an_observation(tmp_path):
    log = tmp_path / "log.jsonl"

    def drop_ladder(answer_id, reply):
        del reply["levelCheck"]["A"]
        return reply

    assert run("full", log, oracle_provider([], mutate=drop_ladder)) == 0
    records = harness.read_log(log)
    assert {r["status"] for r in records} == {"contract_error"}
    assert all(r["error"].startswith("invalid_levelcheck") for r in records)
    calls: list = []
    assert run("full", log, oracle_provider(calls)) == 0
    assert calls == []  # observations are never re-called


# ── The report ──────────────────────────────────────────────────────────────


def _oracle_log(tmp_path, **kw) -> list[dict]:
    log = tmp_path / "log.jsonl"
    for stage in ("smoke", "full", "stability"):
        assert run(stage, log, oracle_provider([], **kw)) == 0
    return harness.read_log(log)


def _gate_status(report: str) -> dict[str, str]:
    statuses = {}
    for line in report.splitlines():
        if line.startswith("| G"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            statuses[cells[0]] = cells[3].strip("*")
    return statuses


def test_a_perfect_judge_passes_every_automatic_gate(tmp_path):
    report, all_pass = harness.build_report(_oracle_log(tmp_path))
    statuses = _gate_status(report)
    assert set(statuses) == {f"G{n}" for n in range(1, 15)}
    assert statuses.pop("G13") == "OWNER"
    assert set(statuses.values()) == {"PASS"}, report
    assert all_pass
    # G13's material is printed for the owner.
    assert "## For the owner (G13)" in report and "fam_01_simple_a1" in report.split("## For the owner (G13)")[1]


def test_the_report_matches_the_pre_registered_dry_run(tmp_path):
    # Scored from the expected verdicts, run 1's overalls are the README's Stage 0b table.
    records = [r for r in _oracle_log(tmp_path) if r["stage"] == "full"]
    by_id = {r["answerId"]: harness.overalls(r) for r in records}
    assert by_id["fam_01_simple_a1"] == {"A1": 9.5, "A2": 7.8, "exam": 7.1, "B1": 5.6, "B2": 4.0}
    assert by_id["env_05_off_topic"]["A1"] == 2.0
    assert by_id["fut_19_b2"] == {level: 10.0 for level in harness.LEVEL_NAMES}


def test_no_records_never_passes(tmp_path):
    report, all_pass = harness.build_report([])
    assert not all_pass
    assert "FAIL" in report


def test_a_judge_that_flips_verdicts_fails_g8_and_g9(tmp_path):
    log = tmp_path / "log.jsonl"
    for stage in ("smoke", "full"):
        run(stage, log, oracle_provider([]))

    def flip_t(answer_id, reply):
        verdicts = reply["levelCheck"]["T"]["verdicts"]
        reply["levelCheck"]["T"]["verdicts"] = ["y", "n", "n", "n"] if verdicts[1] == "y" else ["y", "y", "y", "n"]
        return reply

    run("stability", log, oracle_provider([], mutate=flip_t))
    statuses = _gate_status(harness.build_report(harness.read_log(log))[0])
    assert statuses["G9"] == "FAIL"
    assert statuses["G1"] == "PASS"


def test_g6_and_g11_fail_when_errors_are_missed_or_calls_run_long(tmp_path):
    def forget_errors(answer_id, reply):
        reply["grammar"] = {"critical": [], "polish": []}
        return reply

    statuses = _gate_status(harness.build_report(_oracle_log(tmp_path, mutate=forget_errors, tokens=6500))[0])
    assert statuses["G6"] == "FAIL"
    assert statuses["G11"] == "FAIL"  # 13,000 an answer > 7,500


def test_g10_counts_quotes_before_the_guardrail(tmp_path):
    def invent_quotes(answer_id, reply):
        for entry in reply["levelCheck"].values():
            if "y" in entry["verdicts"]:
                entry["quote"] = "des mots que l'élève n'a jamais dits"
        return reply

    statuses = _gate_status(harness.build_report(_oracle_log(tmp_path, mutate=invent_quotes))[0])
    assert statuses["G10"] == "FAIL"


def test_the_cli_writes_the_report(tmp_path):
    log = tmp_path / "log.jsonl"
    run("smoke", log, oracle_provider([]))
    out = tmp_path / "report.md"
    assert harness.main_cli(["--report", "--log", str(log), "--report-out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# Learn marking gate report — groq")


def test_a_log_from_another_prompt_is_refused(tmp_path):
    log = tmp_path / "log.jsonl"
    run("smoke", log, oracle_provider([]))
    records = harness.read_log(log)
    records[0]["versions"]["systemPromptSha256"] = "an-older-prompt"
    log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    with pytest.raises(SystemExit, match="another prompt"):
        run("full", log, oracle_provider([]))


def test_the_cost_limits_are_the_owners_values():
    # 2026-10-19: max 8,000 per call. 2026-10-23 (Design D): median 7,500 per answer, both calls together.
    assert (harness.SMOKE_MAX_TOKENS, harness.G11_MEDIAN_TOKENS_PER_ANSWER, harness.G11_MAX_TOKENS) == (8000, 7500, 8000)


def test_g11_holds_the_median_per_answer_and_the_max_per_call(tmp_path):
    # 3,700 a call is 7,400 an answer: passes. 3,800 a call is 7,600 an answer: fails, though each call is small.
    for name, tokens, status in (("a", 3700, "PASS"), ("b", 3800, "FAIL")):
        (tmp_path / name).mkdir()
        assert _gate_status(harness.build_report(_oracle_log(tmp_path / name, tokens=tokens))[0])["G11"] == status


def test_g8_counts_an_unscored_answer_as_all_wrong(tmp_path):
    def break_b2(answer_id, reply):
        if answer_id == "fut_19_b2":
            reply["levelCheck"]["O"]["verdicts"][1] = "maybe"  # not y, n or null: a contract error
        return reply

    report, _ = harness.build_report(_oracle_log(tmp_path, mutate=break_b2))
    g8 = next(line for line in report.splitlines() if line.startswith("| G8 "))
    assert "unscored, counted as all wrong: fut_19_b2" in g8
    assert "all 387/409" in g8
    assert _gate_status(report)["G8"] == "PASS"  # 95% still clears 85%
