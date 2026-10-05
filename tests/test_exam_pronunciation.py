"""POST/GET /api/exam/pronunciation (exam-pronunciation plan, Batch 4).

Offline: Azure is mocked with httpx.MockTransport, Whisper and the Supabase
service client are in-memory fakes. Covers the access modes, the cache (no
charge on a hit), consent, the budget-exhausted and failure paths with their
releases, chunking of a long turn, the per-part quota key, request
validation, and user isolation (user_id only from the JWT).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import jwt as pyjwt
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import lib.auth as lib_auth
import lib.consent as consent
import routers.exam_pronunciation as exam_pron
from models.exam_pronunciation import ExamPronunciationGetResponse, ExamPronunciationPostResponse

_SECRET = "test-jwt-secret-at-least-32-characters-long!!"
USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _jwt(sub: str, *, admin: bool = False) -> str:
    claims = {"sub": sub, "exp": int(time.time()) + 3600}
    if admin:
        claims["app_metadata"] = {"role": "admin"}
    return pyjwt.encode(claims, _SECRET, algorithm="HS256")


def _wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00" * int(seconds * 16000))
    return buf.getvalue()


# ── Fakes ────────────────────────────────────────────────────────────────────

class _Res:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self.op, self.row = db, table, [], "select", None

    def select(self, _cols):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def limit(self, _n):
        return self

    def upsert(self, row, on_conflict=None, ignore_duplicates=False):
        self.op, self.row = "upsert", row
        return self

    def execute(self):
        assert self.table == exam_pron.TABLE
        if self.op == "upsert":
            if self.db.fail_insert:
                raise RuntimeError("insert or update violates foreign key constraint")
            key = tuple(self.row[k] for k in ("user_id", "session_id", "turn_key", "assessor_version"))
            if not any(tuple(r[k] for k in ("user_id", "session_id", "turn_key", "assessor_version")) == key
                       for r in self.db.rows):
                self.db.rows.append({**json.loads(json.dumps(self.row)), "created_at": "2026-10-05T10:00:00+00:00"})
            return _Res([])
        return _Res([r for r in self.db.rows if all(r.get(k) == v for k, v in self.filters)])


class _Rpc:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return _Res(self.data)


class FakeDb:
    def __init__(self, *, budget_granted: bool = True):
        self.rows: list[dict] = []
        self.ledger: list[tuple[str, dict]] = []
        self.budget_granted = budget_granted
        self.fail_insert = False
        self._n = 0

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, args):
        self.ledger.append((name, args))
        if name == "reserve_azure_seconds":
            if not self.budget_granted:
                return _Rpc({"granted": False, "reason": "budget_exhausted", "used_seconds": 10, "cap_seconds": 10})
            self._n += 1
            return _Rpc({"granted": True, "reservation_id": f"res-{self._n}", "used_seconds": 0, "cap_seconds": None})
        return _Rpc({"ok": True})

    def ledger_names(self):
        return [n for n, _ in self.ledger]


class Quota:
    """Mirrors consume_ai_quota's replay: a repeated key is granted, not re-charged."""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []
        self.charged: set[tuple[str, str, str]] = set()
        self.released: list[tuple[str, str, str]] = []

    async def consume(self, db, user_id, feature, key):
        self.calls.append((user_id, feature, key))
        self.charged.add((user_id, feature, key))
        return {"granted": True}

    async def release(self, db, user_id, feature, key):
        self.released.append((user_id, feature, key))
        self.charged.discard((user_id, feature, key))


def _reference(request: httpx.Request) -> str:
    header = json.loads(base64.b64decode(request.headers["Pronunciation-Assessment"]))
    return header["ReferenceText"]


def azure_echo(low: dict[str, float] | None = None):
    """Azure stub: assesses every reference word at 90, or `low[word]`
    (reported as a Mispronunciation)."""
    low = low or {}

    def handler(request: httpx.Request) -> httpx.Response:
        words = _reference(request).split()
        out = []
        for i, w in enumerate(words):
            acc = low.get(w, 90.0)
            out.append({
                "Word": w.lower().strip(".,!?"), "AccuracyScore": acc,
                "ErrorType": "Mispronunciation" if w in low else "None",
                "Offset": 2_000_000 + i * 5_000_000, "Duration": 4_000_000,
                "Phonemes": [{"AccuracyScore": acc}],
            })
        return httpx.Response(200, json={
            "RecognitionStatus": "Success", "DisplayText": " ".join(words), "SNR": 24.5,
            "NBest": [{"Confidence": 0.91, "Display": " ".join(words), "AccuracyScore": 85.0,
                       "FluencyScore": 80.0, "CompletenessScore": None, "PronScore": 84.0, "Words": out}],
        })

    return handler


def whisper(text: str, segments: list[dict] | None = None):
    async def _fn(tmp_path, language):
        return {"text": text, "segments": segments or [], "words": [], "source": "groq-whisper"}
    return _fn


class Env:
    def __init__(self, monkeypatch, *, mode="all", db=None, handler=None, whisper_fn=None):
        monkeypatch.setenv("EXAM_PRONUNCIATION_ACCESS", mode)
        monkeypatch.setattr(lib_auth, "SUPABASE_JWT_SECRET", _SECRET)
        self.db = db or FakeDb()
        monkeypatch.setattr(exam_pron, "_db", lambda: self.db)
        self.quota = Quota()
        monkeypatch.setattr(exam_pron, "consume_ai_quota_or_503", self.quota.consume)
        monkeypatch.setattr(exam_pron, "release_ai_quota_grant", self.quota.release)
        exam_pron.configure(whisper_fn or whisper("Je vais au cinéma avec mes amis."), None, lambda: True, None)
        monkeypatch.setenv("AZURE_SPEECH_KEY", "k")
        monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
        self.azure_calls: list[httpx.Request] = []
        self.max_in_flight = 0
        in_flight = 0
        handler = handler or azure_echo()

        async def _handler(request):
            nonlocal in_flight
            self.azure_calls.append(request)
            in_flight += 1
            self.max_in_flight = max(self.max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return handler(request)

        original = httpx.AsyncClient

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)
        router = APIRouter(prefix="/api/exam")
        router.post("/pronunciation", response_model=ExamPronunciationPostResponse)(exam_pron.exam_pronunciation_analyse)
        router.get("/pronunciation", response_model=ExamPronunciationGetResponse)(exam_pron.exam_pronunciation_stored)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def post(self, *, sub=USER_A, admin=False, session_id="s1", part="topic1", turn_key="7",
             transcript="Je vais au cinéma avec mes amis.", seconds=4.0, audio=None, files=None, **extra):
        data = {"session_id": session_id, "part": part, "turn_key": turn_key, "exam_transcript": transcript,
                "fairness_version": "exam-pron-fair-v1", "raw_s": "5.2", **extra}
        if files is None:
            files = {"audio": ("turn.wav", audio if audio is not None else _wav(seconds), "audio/wav")}
        return self.client.post("/api/exam/pronunciation", data=data, files=files or None,
                                headers={"Authorization": f"Bearer {_jwt(sub, admin=admin)}"})

    def get(self, *, sub=USER_A, session_id="s1"):
        return self.client.get("/api/exam/pronunciation", params={"session_id": session_id},
                               headers={"Authorization": f"Bearer {_jwt(sub)}"})


# ── Access modes ─────────────────────────────────────────────────────────────

def test_access_off_by_default(monkeypatch):
    env = Env(monkeypatch)
    monkeypatch.delenv("EXAM_PRONUNCIATION_ACCESS")
    resp = env.post()
    assert resp.status_code == 403
    assert resp.json()["detail"] == {"status": "not_enabled"}
    assert env.get().status_code == 403
    assert env.quota.calls == [] and env.azure_calls == []


def test_unknown_access_value_is_off(monkeypatch):
    env = Env(monkeypatch, mode="everyone")
    assert env.post().status_code == 403


def test_admin_mode_refuses_non_admins_and_serves_admins(monkeypatch):
    env = Env(monkeypatch, mode="admin")
    denied = env.post()
    assert denied.status_code == 403 and denied.json()["detail"] == {"status": "not_enabled"}
    assert env.quota.calls == []
    ok = env.post(admin=True)
    assert ok.status_code == 200 and ok.json()["status"] == "done"


def test_no_guest_access(monkeypatch):
    env = Env(monkeypatch)
    monkeypatch.setenv("GUEST_AI_ENABLED", "1")
    resp = env.client.post("/api/exam/pronunciation", data={"session_id": "s1"})
    assert resp.status_code == 401


# ── Happy path, cache, ledger ────────────────────────────────────────────────

def test_analyses_stores_and_meters_one_turn(monkeypatch):
    env = Env(monkeypatch, handler=azure_echo({"cinéma": 30.0}))
    resp = env.post(pauses_over_2s="1", longest_pause_s="2.4", clipped_ratio="0.002")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done" and body["cached"] is False
    turn = body["turn"]
    assert turn["part"] == "topic1" and turn["turnKey"] == "7"
    assert turn["assessorVersion"] == exam_pron.EXAM_PRONUNCIATION_ASSESSOR_VERSION
    assert turn["fairnessVersion"] == "exam-pron-fair-v1"
    assert turn["trimmedS"] == 4.0 and turn["rawS"] == 5.2
    assert turn["pauseStats"] == {"pausesOver2s": 1, "longestPauseS": 2.4}
    assert turn["clippedRatio"] == 0.002
    assert turn["snrDb"] == 24.5 and turn["azureConfidence"] == 0.91
    cinema = next(w for w in turn["words"] if w["word"] == "cinéma")
    assert cinema["errorType"] == "mispronounced" and cinema["accuracyScore"] == 30.0
    assert cinema["recognizersAgree"] is True and cinema["examWord"] == "cinéma"
    assert cinema["suppressed"] == []
    # No overall score of any kind is stored or returned.
    for forbidden in ("score", "subScores", "fluency", "pronScore"):
        assert forbidden not in turn
        assert forbidden not in env.db.rows[0]["result"]
    # Azure: one freeform call (no miscue) against the Whisper reference.
    assert len(env.azure_calls) == 1
    assert _reference(env.azure_calls[0]) == "Je vais au cinéma avec mes amis."
    assert json.loads(base64.b64decode(env.azure_calls[0].headers["Pronunciation-Assessment"]))["EnableMiscue"] is False
    # Ledger: source exam, with the session/part/turn coordinates, settled.
    assert env.db.ledger_names() == ["reserve_azure_seconds", "settle_azure_seconds"]
    reserve = env.db.ledger[0][1]
    assert reserve["p_source"] == "exam" and reserve["p_seconds"] == 4.0
    assert (reserve["p_session_id"], reserve["p_part"], reserve["p_turn_key"]) == ("s1", "topic1", "7")
    assert reserve["p_user_id"] == USER_A
    # Quota: the part key.
    assert env.quota.calls == [(USER_A, "exam_pronunciation", "exam-pron:s1:topic1")]
    assert env.quota.released == []
    assert len(env.db.rows) == 1 and env.db.rows[0]["user_id"] == USER_A


def test_cache_hit_makes_no_azure_call_and_no_charge(monkeypatch):
    env = Env(monkeypatch)
    first = env.post()
    second = env.post()
    assert first.json()["status"] == second.json()["status"] == "done"
    assert second.json()["cached"] is True
    assert second.json()["turn"]["words"] == first.json()["turn"]["words"]
    assert len(env.azure_calls) == 1
    assert len(env.quota.calls) == 1
    assert env.db.ledger_names().count("reserve_azure_seconds") == 1


def test_per_part_quota_key_replays_across_turns(monkeypatch):
    env = Env(monkeypatch)
    assert env.post(turn_key="3", part="rolePlay").json()["status"] == "done"
    assert env.post(turn_key="5", part="rolePlay").json()["status"] == "done"
    assert env.post(turn_key="9", part="topic2").json()["status"] == "done"
    keys = [k for _, _, k in env.quota.calls]
    assert keys == ["exam-pron:s1:rolePlay", "exam-pron:s1:rolePlay", "exam-pron:s1:topic2"]
    assert len(env.quota.charged) == 2  # one grant per part


# ── Consent ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["pending", "missing"])
def test_consent_required_before_anything_else(monkeypatch, status):
    env = Env(monkeypatch)

    async def _fetch(db, user_id):
        return consent._MISSING if status == "missing" else status

    monkeypatch.setattr(consent, "_fetch_consent_status", _fetch)
    resp = env.post()
    assert resp.status_code == 403
    assert resp.json()["detail"] == {"error": "consent_required"}
    assert env.quota.calls == [] and env.azure_calls == [] and env.db.ledger == []


# ── Budget and failure ───────────────────────────────────────────────────────

def test_budget_exhausted_is_a_200_status_and_releases_the_grant(monkeypatch):
    env = Env(monkeypatch, db=FakeDb(budget_granted=False))
    resp = env.post()
    assert resp.status_code == 200
    assert resp.json() == {"status": "budget_exhausted", "turn": None, "cached": False, "reason": None}
    assert env.azure_calls == []
    assert env.quota.released == env.quota.calls
    assert env.db.rows == []


def test_azure_quota_exceeded_maps_to_budget_exhausted(monkeypatch):
    env = Env(monkeypatch, handler=lambda r: httpx.Response(403, text="Out of call volume quota."))
    resp = env.post()
    assert resp.json()["status"] == "budget_exhausted"
    assert env.db.ledger_names() == ["reserve_azure_seconds", "release_azure_seconds"]
    assert env.quota.released == env.quota.calls


def test_failure_releases_reservation_and_grant(monkeypatch):
    env = Env(monkeypatch, handler=lambda r: httpx.Response(500, text="boom"))
    resp = env.post()
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    assert env.db.ledger_names() == ["reserve_azure_seconds", "release_azure_seconds"]
    assert env.quota.released == env.quota.calls
    assert env.db.rows == []


def test_failure_keeps_the_grant_when_another_turn_of_the_part_is_stored(monkeypatch):
    env = Env(monkeypatch)
    assert env.post(turn_key="3").json()["status"] == "done"
    monkeypatch.setattr(httpx, "AsyncClient", _always(500))
    assert env.post(turn_key="4").json()["status"] == "failed"
    assert env.quota.released == []


def _always(status):
    real = httpx._client.AsyncClient  # the class itself; httpx.AsyncClient is patched

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(status, text="boom"))
        return real(*args, **kwargs)

    return patched


def test_transcription_failure_is_failed_not_no_speech(monkeypatch):
    async def _boom(tmp_path, language):
        raise RuntimeError("groq down")

    env = Env(monkeypatch, whisper_fn=_boom)
    resp = env.post()
    assert resp.json()["status"] == "failed" and resp.json()["reason"] == "transcription_failed"
    assert env.azure_calls == [] and env.quota.released == env.quota.calls


def test_silence_is_no_speech_not_stored_not_billed(monkeypatch):
    env = Env(monkeypatch, whisper_fn=whisper(""))
    body = env.post().json()
    assert body["status"] == "done"
    assert body["turn"]["couldNotAssess"] is True
    assert body["turn"]["couldNotAssessReason"] == "no_speech_recognized"
    assert env.azure_calls == [] and env.db.rows == [] and env.quota.released == env.quota.calls


def test_insert_failure_leaves_no_row_and_is_failed(monkeypatch):
    db = FakeDb()
    db.fail_insert = True
    env = Env(monkeypatch, db=db)
    body = env.post().json()
    assert body["status"] == "failed" and body["reason"] == "store_failed"
    assert db.rows == []


def test_azure_not_configured_is_503_before_any_charge(monkeypatch):
    env = Env(monkeypatch)
    monkeypatch.delenv("AZURE_SPEECH_KEY")
    resp = env.post()
    assert resp.status_code == 503
    assert env.quota.calls == []


# ── Chunking ─────────────────────────────────────────────────────────────────

def test_a_40s_turn_is_two_serial_chunks(monkeypatch):
    segments = [
        {"start": 0.0, "end": 19.0, "text": "Le week-end dernier je suis allé au parc."},
        {"start": 19.0, "end": 40.0, "text": "Demain je vais jouer au foot."},
    ]
    text = " ".join(s["text"] for s in segments)
    env = Env(monkeypatch, whisper_fn=whisper(text, segments))
    body = env.post(seconds=40.0, transcript=text).json()
    assert body["status"] == "done"
    assert len(env.azure_calls) == 2
    assert env.max_in_flight == 1
    assert [_reference(r) for r in env.azure_calls] == [s["text"] for s in segments]
    reserves = [a for n, a in env.db.ledger if n == "reserve_azure_seconds"]
    assert [r["p_seconds"] for r in reserves] == [19.0, 21.0]
    assert env.db.ledger_names().count("settle_azure_seconds") == 2
    turn = body["turn"]
    assert turn["chunkCount"] == 2 and turn["chunksFailed"] == 0
    # Second chunk's words are re-based onto the whole clip.
    second_chunk_first = turn["words"][len(segments[0]["text"].split())]
    assert second_chunk_first["offsetMs"] == 19_000 + 200
    assert all(w["recognizersAgree"] is True for w in turn["words"])


def test_plan_chunks_on_whisper_boundaries():
    data = {"text": "a b", "segments": [{"start": 0, "end": 20, "text": "a"}, {"start": 20, "end": 50, "text": "b"}]}
    planned = exam_pron.plan_chunks(data, 50.0)
    # (20, 50) is over Azure's 30 s limit, so it is cut evenly; only the piece
    # holding the segment's midpoint gets its text.
    assert [w for w, _ in planned] == [(0.0, 20), (20.0, 35.0), (35.0, 50.0)]
    assert [ref for _, ref in planned] == ["a", "", "b"]
    assert exam_pron.plan_chunks({"text": "court"}, 12.0) == [((0.0, 12.0), "court")]


def test_words_near_an_internal_seam_are_flagged():
    run = exam_pron._ChunkRun(raw_results=[
        {"provider": "azure", "couldNotAssess": False, "score": 80, "subScores": {"accuracy": 80, "fluency": 80},
         "words": [{"word": "fin", "accuracyScore": 40, "errorType": "mispronounced", "offsetMs": 19_800, "durationMs": 150}]},
        {"provider": "azure", "couldNotAssess": False, "score": 80, "subScores": {"accuracy": 80, "fluency": 80},
         "words": [{"word": "début", "accuracyScore": 40, "errorType": "mispronounced", "offsetMs": 2_000, "durationMs": 300}]},
    ])
    planned = [((0.0, 20.0), "fin"), ((20.0, 30.0), "début")]
    result = exam_pron.build_evidence(run, planned, exam_transcript="fin début", single_recognizer=False)
    assert [w["nearChunkBoundary"] for w in result["words"]] == [True, False]
    assert result["words"][0]["suppressed"] == ["near_seam"]


# ── Recognition agreement ────────────────────────────────────────────────────

def test_recogniser_disagreement_is_suppressed(monkeypatch):
    env = Env(monkeypatch, whisper_fn=whisper("Je vais au cinéma avec mes amis."),
              handler=azure_echo({"cinéma": 20.0}))
    body = env.post(transcript="Je vais au sinéma avec mes amis.").json()
    word = next(w for w in body["turn"]["words"] if w["word"] == "cinéma")
    assert word["recognizersAgree"] is False and word["examWord"] == "sinéma"
    assert word["suppressed"] == ["asr_disagreement"]
    # The stored suppressed column lists exactly the words carrying reasons.
    stored = env.db.rows[0]["suppressed"]
    assert {"index": 3, "word": "cinéma", "reasons": ["asr_disagreement"]} in stored
    assert all(entry["reasons"] for entry in stored)


def test_single_recogniser_mode_has_no_agreement_signal(monkeypatch):
    env = Env(monkeypatch)
    turn = env.post(recognizer="whisper", transcript="Complètement autre chose").json()["turn"]
    assert turn["singleRecognizer"] is True
    assert all(w["recognizersAgree"] is None for w in turn["words"])
    assert all("asr_disagreement" not in w["suppressed"] for w in turn["words"])


def test_alignment_and_structural_suppression():
    aligned = exam_pron.align_to_exam_transcript(["j'ai", "un", "chien", "15"], "J’ai un chat 15")
    assert aligned == [("J'ai", True), ("un", True), ("chat", False), ("15", True)]
    assert exam_pron.suppression_reasons({"word": "un", "recognizersAgree": True}) == ["short_word"]
    assert exam_pron.suppression_reasons({"word": "15", "recognizersAgree": True}) == ["short_word", "number"]


# ── Validation ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("kwargs", "status"), [
    ({"files": {}}, 422),                       # a typed turn: no audio
    ({"transcript": "   "}, 422),               # empty turn
    ({"audio": _wav(0)}, 422),                  # empty recording
    ({"audio": b"webm-bytes"}, 415),            # not the normalised WAV
    ({"part": "greeting"}, 422),
    ({"turn_key": "abc"}, 422),
    ({"recognizer": "siri"}, 422),
    ({"clipped_ratio": "1.5"}, 422),
    ({"seconds": 181.0}, 413),
])
def test_rejected_requests_never_charge(monkeypatch, kwargs, status):
    env = Env(monkeypatch)
    resp = env.post(**kwargs)
    assert resp.status_code == status
    assert env.quota.calls == [] and env.azure_calls == []


# ── Isolation: user_id comes only from the JWT ───────────────────────────────

def test_a_form_user_id_is_ignored(monkeypatch):
    env = Env(monkeypatch)
    assert env.post(sub=USER_B, user_id=USER_A).json()["status"] == "done"
    assert [r["user_id"] for r in env.db.rows] == [USER_B]
    assert env.quota.calls[0][0] == USER_B
    assert env.db.ledger[0][1]["p_user_id"] == USER_B


def test_another_user_with_the_same_session_id_gets_separate_rows(monkeypatch):
    env = Env(monkeypatch)
    a = env.post(sub=USER_A).json()
    a_row = json.loads(json.dumps(env.db.rows[0]))
    b = env.post(sub=USER_B).json()
    assert a["status"] == b["status"] == "done"
    assert b["cached"] is False  # A's row is not B's cache
    assert len(env.azure_calls) == 2
    assert sorted(r["user_id"] for r in env.db.rows) == [USER_A, USER_B]
    assert env.db.rows[0] == a_row  # A's row untouched


def test_get_returns_only_the_callers_rows_and_never_analyses(monkeypatch):
    env = Env(monkeypatch)
    env.post(sub=USER_A, turn_key="3")
    env.post(sub=USER_A, turn_key="12", part="topic2")
    calls_before = len(env.azure_calls)

    mine = env.get(sub=USER_A)
    assert mine.status_code == 200
    assert [t["turnKey"] for t in mine.json()["turns"]] == ["3", "12"]

    theirs = env.get(sub=USER_B)
    assert theirs.status_code == 200
    assert theirs.json()["turns"] == []
    assert len(env.azure_calls) == calls_before
