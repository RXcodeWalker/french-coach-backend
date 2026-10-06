"""Azure Speech metering (exam-pronunciation plan §4, Batch 1).

Two layers:

1. lib/azure_budget.py — WAV-header seconds and the reserve/settle/release
   wrapper, against a fake service-role client.
2. The SQL itself (20261005090000_azure_speech_usage_and_budget.sql) — reserve
   to the cap, NULL cap = unlimited, settle, release, UTC month rollover, the
   advisory-lock race, and client grants — against a throwaway local Postgres
   cluster with a minimal stub of the tables the migration references
   (profiles, ai_quota_limits, the three API roles). Skipped when no Postgres
   server binaries are installed. The full-stack equivalent is
   supabase/tests/azure_budget.test.mjs (local `npx supabase start` only).
"""

from __future__ import annotations

import asyncio
import glob
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.azure_budget import (
    UNKNOWN_DURATION_SECONDS,
    AzureReservation,
    billable_seconds,
    release_azure_seconds,
    reserve_azure_seconds,
    settle_azure_seconds,
    wav_duration_seconds,
)

_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
MIGRATIONS = [
    _MIGRATIONS_DIR / "20261005090000_azure_speech_usage_and_budget.sql",
    _MIGRATIONS_DIR / "20261006090000_azure_speech_probe_source.sql",
]


def _wav(seconds: float, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


# ── WAV header ───────────────────────────────────────────────────────────────

def test_wav_duration_reads_the_header():
    assert wav_duration_seconds(_wav(2.5)) == 2.5
    assert wav_duration_seconds(_wav(1.0, rate=48000)) == 1.0


def test_wav_duration_walks_past_extra_chunks():
    raw = _wav(1.5)
    # Insert a LIST chunk between fmt and data, as some encoders do.
    fmt_end = 12 + 8 + 16
    extra = b"LIST" + (6).to_bytes(4, "little") + b"INFOab"
    patched = raw[:fmt_end] + extra + raw[fmt_end:]
    patched = patched[:4] + (len(patched) - 8).to_bytes(4, "little") + patched[8:]
    assert wav_duration_seconds(patched) == 1.5


def test_wav_duration_measures_a_truncated_data_chunk_by_bytes_present():
    raw = _wav(2.0)
    assert wav_duration_seconds(raw[: len(raw) // 2 + 22]) == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize("raw", [b"", b"\x1aE\xdf\xa3webm-ish", b"OggS" + b"\x00" * 40, b"RIFF\x00\x00\x00\x00AVI "])
def test_wav_duration_is_none_for_non_wav(raw):
    assert wav_duration_seconds(raw) is None


def test_billable_seconds_prefers_the_header_then_the_estimate_then_the_cap():
    assert billable_seconds(_wav(3.0), 9.0) == (3.0, True)
    assert billable_seconds(b"webm", 7.25) == (7.25, False)
    assert billable_seconds(b"webm", 120.0) == (UNKNOWN_DURATION_SECONDS, False)
    assert billable_seconds(b"webm") == (UNKNOWN_DURATION_SECONDS, False)


# ── Wrapper against a fake service-role client ───────────────────────────────

class _FakeRpc:
    def __init__(self, data=None, exc=None):
        self._data = data
        self._exc = exc

    def execute(self):
        if self._exc:
            raise self._exc
        return type("R", (), {"data": self._data})()


class _FakeDb:
    def __init__(self, responses=None, exc=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}
        self.exc = exc

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _FakeRpc(self.responses.get(name, {"ok": True}), self.exc)


def test_reserve_granted_then_settle():
    db = _FakeDb({"reserve_azure_seconds": {"granted": True, "reservation_id": "r-1", "used_seconds": 4, "cap_seconds": None}})
    r = asyncio.run(reserve_azure_seconds(db, user_id="u-1", source="learn", seconds=4.0, session_id="s", part="topic1", turn_key="7"))
    assert r.granted and r.metered and r.reservation_id == "r-1"
    assert db.calls[0] == ("reserve_azure_seconds", {
        "p_user_id": "u-1", "p_source": "learn", "p_seconds": 4.0,
        "p_session_id": "s", "p_part": "topic1", "p_turn_key": "7",
    })
    asyncio.run(settle_azure_seconds(db, r, 3.5))
    assert db.calls[1] == ("settle_azure_seconds", {"p_reservation_id": "r-1", "p_seconds": 3.5})


def test_release_calls_the_rpc():
    db = _FakeDb()
    asyncio.run(release_azure_seconds(db, AzureReservation(granted=True, reservation_id="r-9", seconds=2, source="lab")))
    assert db.calls == [("release_azure_seconds", {"p_reservation_id": "r-9"})]


def test_reserve_denied_is_never_overridden():
    db = _FakeDb({"reserve_azure_seconds": {"granted": False, "reason": "budget_exhausted", "used_seconds": 100, "cap_seconds": 100}})
    r = asyncio.run(reserve_azure_seconds(db, user_id="u-1", source="learn", seconds=4.0))
    assert not r.granted
    assert r.reason == "budget_exhausted"
    assert r.cap_seconds == 100


def test_guest_usage_is_counted_but_attributed_to_no_one():
    db = _FakeDb({"reserve_azure_seconds": {"granted": True, "reservation_id": "r-2"}})
    asyncio.run(reserve_azure_seconds(db, user_id="guest:1.2.3.4", source="learn", seconds=1.0))
    assert db.calls[0][1]["p_user_id"] is None


@pytest.mark.parametrize("db", [None, _FakeDb(exc=RuntimeError("supabase down")), _FakeDb({"reserve_azure_seconds": {"weird": 1}})])
def test_ledger_unavailable_fails_open_unmetered(db):
    r = asyncio.run(reserve_azure_seconds(db, user_id="u-1", source="learn", seconds=4.0))
    assert r.granted and not r.metered
    # Nothing to settle or release — and neither raises.
    asyncio.run(settle_azure_seconds(db, r))
    asyncio.run(release_azure_seconds(db, r))


def test_settle_and_release_never_raise():
    db = _FakeDb(exc=RuntimeError("boom"))
    r = AzureReservation(granted=True, reservation_id="r-3", seconds=1, source="learn")
    asyncio.run(settle_azure_seconds(db, r))
    asyncio.run(release_azure_seconds(db, r))


def test_unknown_source_is_a_programming_error():
    with pytest.raises(ValueError):
        asyncio.run(reserve_azure_seconds(_FakeDb(), user_id="u", source="bogus", seconds=1.0))


def test_every_ledger_event_logs_one_structured_line(caplog):
    caplog.set_level("INFO", logger="uvicorn.error")
    db = _FakeDb({"reserve_azure_seconds": {"granted": True, "reservation_id": "r-4"}})
    r = asyncio.run(reserve_azure_seconds(db, user_id="u", source="shadowing", seconds=2.0))
    asyncio.run(settle_azure_seconds(db, r, 2.0))
    lines = [rec.getMessage() for rec in caplog.records if rec.getMessage().startswith("azure_speech_usage ")]
    events = [json.loads(line.split(" ", 1)[1])["event"] for line in lines]
    assert events == ["reserved", "settled"]


# ── The SQL, against a throwaway Postgres ────────────────────────────────────

def _pg_bin_dir() -> str | None:
    found = shutil.which("initdb")
    if found:
        return os.path.dirname(found)
    for d in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if os.path.exists(os.path.join(d, "initdb")) and os.path.exists(os.path.join(d, "pg_ctl")):
            return d
    return None


def _as_pg_user() -> list[str] | None:
    """initdb refuses to run as root; drop to the `postgres` user if needed."""
    if os.geteuid() != 0:
        return []
    if shutil.which("runuser"):
        try:
            subprocess.run(["id", "postgres"], check=True, capture_output=True)
            return ["runuser", "-u", "postgres", "--"]
        except subprocess.CalledProcessError:
            return None
    return None


_STUB_SCHEMA = """
CREATE ROLE anon NOLOGIN;
CREATE ROLE authenticated NOLOGIN;
CREATE ROLE service_role NOLOGIN;
CREATE TABLE public.profiles (id uuid PRIMARY KEY);
CREATE TABLE public.ai_quota_limits (feature text PRIMARY KEY, daily_limit integer NOT NULL);
"""


class _Pg:
    def __init__(self, bin_dir: str, prefix: list[str], sock: str, port: int):
        self.bin_dir, self.prefix, self.sock, self.port = bin_dir, prefix, sock, port

    def psql(self, sql: str) -> str:
        out = subprocess.run(
            [*self.prefix, os.path.join(self.bin_dir, "psql"), "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1",
             "-h", self.sock, "-p", str(self.port), "-U", "postgres", "-d", "postgres", "-c", sql],
            check=True, capture_output=True, text=True,
        )
        return out.stdout.strip()

    def json(self, sql: str):
        return json.loads(self.psql(sql))


@pytest.fixture(scope="module")
def pg():
    bin_dir = _pg_bin_dir()
    prefix = _as_pg_user()
    if bin_dir is None or prefix is None:
        pytest.skip("no local Postgres server binaries (or no unprivileged user to run them)")
    root = tempfile.mkdtemp(prefix="azure-budget-pg-")
    os.chmod(root, 0o777)
    data, sock = os.path.join(root, "data"), os.path.join(root, "sock")
    os.mkdir(sock)
    os.chmod(sock, 0o777)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    run = lambda *a: subprocess.run([*prefix, *a], check=True, capture_output=True, text=True)  # noqa: E731
    try:
        run(os.path.join(bin_dir, "initdb"), "-D", data, "-A", "trust", "-U", "postgres")
        run(os.path.join(bin_dir, "pg_ctl"), "-D", data, "-w", "-l", os.path.join(root, "log"),
            "-o", f"-k {sock} -p {port} -c listen_addresses=''", "start")
    except subprocess.CalledProcessError as e:
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip(f"could not start a local Postgres: {e.stderr[-300:]}")
    db = _Pg(bin_dir, prefix, sock, port)
    try:
        db.psql(_STUB_SCHEMA)
        for migration in MIGRATIONS:
            db.psql(migration.read_text())
        yield db
    finally:
        subprocess.run([*prefix, os.path.join(bin_dir, "pg_ctl"), "-D", data, "-m", "immediate", "stop"],
                       capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def ledger(pg):
    pg.psql("TRUNCATE public.azure_speech_usage; UPDATE public.azure_speech_budget SET cap_seconds = NULL;")
    return pg


def _reserve(pg: _Pg, seconds: float, *, user: str | None = None, source: str = "learn", session: str | None = None) -> dict:
    u = f"'{user}'::uuid" if user else "NULL"
    sess = f"'{session}'" if session else "NULL"
    return pg.json(f"SELECT public.reserve_azure_seconds({u}, '{source}', {seconds}, {sess}, NULL, NULL)")


def _month_total(pg: _Pg) -> float:
    return float(pg.json("SELECT public.azure_speech_usage_summary()")["total_seconds"])


def test_sql_null_cap_is_unlimited(ledger):
    for _ in range(3):
        assert _reserve(ledger, 1_000_000)["granted"] is True
    assert _month_total(ledger) == 3_000_000


def test_sql_reserve_up_to_the_cap_then_deny(ledger):
    ledger.psql("UPDATE public.azure_speech_budget SET cap_seconds = 100")
    assert _reserve(ledger, 60)["granted"] is True
    denied = _reserve(ledger, 50)
    assert denied["granted"] is False and denied["reason"] == "budget_exhausted"
    assert float(denied["used_seconds"]) == 60 and denied["cap_seconds"] == 100
    assert _reserve(ledger, 40)["granted"] is True  # exactly at the cap
    assert _reserve(ledger, 0.001)["granted"] is False


def test_sql_settle_records_measured_seconds_once(ledger):
    rid = _reserve(ledger, 30)["reservation_id"]
    assert ledger.json(f"SELECT public.settle_azure_seconds('{rid}', 12.5)")["settled"] is True
    assert ledger.psql(f"SELECT status || ':' || seconds FROM public.azure_speech_usage WHERE id = '{rid}'") == "settled:12.500"
    assert ledger.json(f"SELECT public.settle_azure_seconds('{rid}', 99)")["settled"] is False
    assert _month_total(ledger) == 12.5


def test_sql_release_frees_the_reservation(ledger):
    ledger.psql("UPDATE public.azure_speech_budget SET cap_seconds = 50")
    rid = _reserve(ledger, 50)["reservation_id"]
    assert _reserve(ledger, 10)["granted"] is False
    assert ledger.json(f"SELECT public.release_azure_seconds('{rid}')")["released"] is True
    assert _reserve(ledger, 10)["granted"] is True
    # The released row stays, for the record.
    assert ledger.psql("SELECT count(*) FROM public.azure_speech_usage WHERE status = 'released'") == "1"


def test_sql_month_rollover_ignores_last_month(ledger):
    ledger.psql("UPDATE public.azure_speech_budget SET cap_seconds = 100")
    ledger.psql(
        "INSERT INTO public.azure_speech_usage (source, seconds, status, created_at) "
        "VALUES ('learn', 5000, 'settled', public._azure_speech_month_start() - interval '1 second')"
    )
    assert _month_total(ledger) == 0
    assert _reserve(ledger, 100)["granted"] is True


def test_sql_probe_source_is_metered(ledger):
    assert _reserve(ledger, 4, source="probe")["granted"] is True
    assert ledger.json("SELECT public.azure_speech_usage_summary()")["by_source"] == {"probe": 4}


def test_sql_user_attribution_survives_account_deletion(ledger):
    user = str(uuid.uuid4())
    ledger.psql(f"INSERT INTO public.profiles (id) VALUES ('{user}')")
    rid = _reserve(ledger, 3, user=user, source="exam", session="sess-1")["reservation_id"]
    assert ledger.psql(f"SELECT user_id FROM public.azure_speech_usage WHERE id = '{rid}'") == user
    ledger.psql(f"DELETE FROM public.profiles WHERE id = '{user}'")
    assert ledger.psql(f"SELECT coalesce(user_id::text, 'null') FROM public.azure_speech_usage WHERE id = '{rid}'") == "null"
    assert _month_total(ledger) == 3


def test_sql_unknown_profile_is_recorded_as_null_not_an_error(ledger):
    r = _reserve(ledger, 2, user=str(uuid.uuid4()))
    assert r["granted"] is True


def test_sql_rejects_unknown_source_and_negative_seconds(ledger):
    with pytest.raises(subprocess.CalledProcessError):
        _reserve(ledger, 1, source="bogus")
    with pytest.raises(subprocess.CalledProcessError):
        _reserve(ledger, -1)


def test_sql_summary_by_source_and_per_exam(ledger):
    _reserve(ledger, 10, source="learn")
    _reserve(ledger, 4, source="exam", session="sess-a")
    _reserve(ledger, 6, source="exam", session="sess-a")
    _reserve(ledger, 2, source="exam", session="sess-b")
    summary = ledger.json("SELECT public.azure_speech_usage_summary()")
    assert {k: float(v) for k, v in summary["by_source"].items()} == {"learn": 10, "exam": 12}
    assert [(e["session_id"], float(e["seconds"])) for e in summary["exams"]] == [("sess-a", 10), ("sess-b", 2)]
    assert summary["cap_seconds"] is None


def test_sql_concurrent_reservations_cannot_overshoot_the_cap(ledger):
    ledger.psql("UPDATE public.azure_speech_budget SET cap_seconds = 50")
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: _reserve(ledger, 10), range(10)))
    assert sum(1 for r in results if r["granted"]) == 5
    assert _month_total(ledger) == 50


def test_sql_clients_hold_no_privilege(ledger):
    for role in ("anon", "authenticated"):
        for table in ("azure_speech_usage", "azure_speech_budget"):
            assert ledger.psql(f"SELECT has_table_privilege('{role}', 'public.{table}', 'SELECT')") == "f"
        for fn in (
            "public.reserve_azure_seconds(uuid, text, numeric, text, text, text)",
            "public.settle_azure_seconds(uuid, numeric)",
            "public.release_azure_seconds(uuid)",
            "public.azure_speech_usage_summary()",
        ):
            assert ledger.psql(f"SELECT has_function_privilege('{role}', '{fn}', 'EXECUTE')") == "f"
    assert ledger.psql(
        "SELECT has_function_privilege('service_role', 'public.reserve_azure_seconds(uuid, text, numeric, text, text, text)', 'EXECUTE')"
    ) == "t"


def test_sql_seeds_the_exam_pronunciation_quota_row(ledger):
    assert ledger.psql("SELECT daily_limit FROM public.ai_quota_limits WHERE feature = 'exam_pronunciation'") == "1000"
    assert ledger.psql("SELECT coalesce(cap_seconds::text, 'null') FROM public.azure_speech_budget") == "null"
