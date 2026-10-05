"""Azure Speech seconds metering: WAV-header duration, plus the
reserve -> settle | release cycle against the azure_speech_usage ledger
(20261005090000_azure_speech_usage_and_budget.sql).

Every Azure HTTP request is wrapped in one reservation:

    r = await reserve_azure_seconds(db, user_id=..., source="learn", seconds=s)
    if not r.granted:          # the monthly cap is set and reached
        ...skip Azure, report budget_exhausted...
    ...call Azure...
    await settle_azure_seconds(db, r, s)    # Azure processed the audio
    await release_azure_seconds(db, r)      # no billable call happened

The cap is a single config row (azure_speech_budget.cap_seconds); NULL means
unlimited, which is how it ships. Numbers are deliberately not chosen here.

Failure posture: a ledger/RPC failure fails OPEN (the reservation comes back
`metered=False` and the Azure call proceeds), logged at WARNING. Every
signed-in caller has already passed `consume_ai_quota_or_503`, which fails
CLOSED on the same Supabase outage, so an unreachable ledger cannot turn into
unmetered spend for them. Guests never reach Supabase; their in-process cap
(lib/guest.py) is the backstop. An explicit `granted: false` from the RPC is
never overridden.

Seconds are measured from the uploaded WAV header (the client normalises to
16 kHz mono WAV). That figure is authoritative. For a non-WAV upload the
header can't be read, so the reservation uses `fallback_seconds` (a caller's
estimate, else the REST short-audio cap of 30 s — the most one request can
bill).
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from dataclasses import dataclass
from typing import Any

from lib.guest import is_guest

log = logging.getLogger("uvicorn.error")

AZURE_SPEECH_SOURCES = ("learn", "exam", "repair", "lab", "shadowing")

# Azure's REST short-audio endpoint rejects anything longer, so no single
# request can bill more than this. Used only when the duration is unknown.
UNKNOWN_DURATION_SECONDS = 30.0


def wav_duration_seconds(raw: bytes) -> float | None:
    """Duration of a RIFF/WAVE PCM file from its header: data-chunk bytes /
    byte rate. None when `raw` is not a parseable WAV (webm, ogg, mp4, junk).

    Walks the chunk list rather than assuming the canonical 44-byte header,
    because encoders may insert LIST/fact chunks before `data`. A data chunk
    whose declared size runs past the end of the buffer (a streaming writer's
    0xFFFFFFFF placeholder, or a truncated upload) is measured by the bytes
    actually present.
    """
    if len(raw) < 12 or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return None
    pos = 12
    byte_rate: int | None = None
    while pos + 8 <= len(raw):
        chunk_id = raw[pos:pos + 4]
        (size,) = struct.unpack("<I", raw[pos + 4:pos + 8])
        body = pos + 8
        if chunk_id == b"fmt ":
            if size < 16 or body + 16 > len(raw):
                return None
            byte_rate = struct.unpack("<I", raw[body + 8:body + 12])[0]
        elif chunk_id == b"data":
            if not byte_rate:
                return None
            data_bytes = min(size, len(raw) - body)
            return round(data_bytes / byte_rate, 3)
        pos = body + size + (size & 1)  # chunks are word-aligned
    return None


def billable_seconds(raw: bytes, fallback_seconds: float | None = None) -> tuple[float, bool]:
    """(seconds, measured). `measured` is True when the WAV header was read."""
    measured = wav_duration_seconds(raw)
    if measured is not None:
        return measured, True
    if fallback_seconds is not None and fallback_seconds > 0:
        return round(min(float(fallback_seconds), UNKNOWN_DURATION_SECONDS), 3), False
    return UNKNOWN_DURATION_SECONDS, False


@dataclass
class AzureReservation:
    granted: bool
    reservation_id: str | None = None
    seconds: float = 0.0
    source: str = ""
    # False when the ledger couldn't be reached (fail-open) — nothing to settle.
    metered: bool = True
    reason: str | None = None
    used_seconds: float | None = None
    cap_seconds: int | None = None


def _ledger_user_id(user_id: str | None) -> str | None:
    # Guests (guest:<ip>) have no profile row; their usage is still counted,
    # attributed to no one.
    if not user_id or is_guest(user_id):
        return None
    return user_id


def _log_usage(event: str, **fields: Any) -> None:
    """One structured line per ledger event, greppable as `azure_speech_usage`."""
    log.info("azure_speech_usage %s", json.dumps({"event": event, **fields}, default=str, sort_keys=True))


async def reserve_azure_seconds(
    db,
    *,
    user_id: str | None,
    source: str,
    seconds: float,
    session_id: str | None = None,
    part: str | None = None,
    turn_key: str | None = None,
) -> AzureReservation:
    if source not in AZURE_SPEECH_SOURCES:
        raise ValueError(f"unknown Azure speech source: {source!r}")
    if db is None:
        _log_usage("reserve_unmetered", source=source, seconds=seconds, reason="ledger_unavailable")
        return AzureReservation(granted=True, seconds=seconds, source=source, metered=False, reason="ledger_unavailable")
    try:
        res = await asyncio.to_thread(
            lambda: db.rpc(
                "reserve_azure_seconds",
                {
                    "p_user_id": _ledger_user_id(user_id),
                    "p_source": source,
                    "p_seconds": seconds,
                    "p_session_id": session_id,
                    "p_part": part,
                    "p_turn_key": turn_key,
                },
            ).execute()
        )
        data = res.data or {}
        if "granted" not in data:
            raise ValueError(f"malformed reserve_azure_seconds response: {data!r}")
    except Exception as e:
        log.warning("azure_budget: reserve_azure_seconds failed (proceeding unmetered): %s", e)
        _log_usage("reserve_unmetered", source=source, seconds=seconds, reason="ledger_error")
        return AzureReservation(granted=True, seconds=seconds, source=source, metered=False, reason="ledger_error")

    if not data.get("granted"):
        _log_usage(
            "budget_exhausted", source=source, seconds=seconds,
            used_seconds=data.get("used_seconds"), cap_seconds=data.get("cap_seconds"),
        )
        return AzureReservation(
            granted=False, seconds=seconds, source=source,
            reason=data.get("reason") or "budget_exhausted",
            used_seconds=data.get("used_seconds"), cap_seconds=data.get("cap_seconds"),
        )

    reservation = AzureReservation(
        granted=True,
        reservation_id=str(data.get("reservation_id")) if data.get("reservation_id") else None,
        seconds=seconds,
        source=source,
        used_seconds=data.get("used_seconds"),
        cap_seconds=data.get("cap_seconds"),
    )
    _log_usage("reserved", source=source, seconds=seconds, reservation_id=reservation.reservation_id,
               session_id=session_id, part=part, turn_key=turn_key)
    return reservation


async def settle_azure_seconds(db, reservation: AzureReservation, seconds: float | None = None) -> None:
    """Azure processed the audio. Never raises — a failed settle leaves the
    row 'reserved', which still counts the reserved seconds."""
    final = reservation.seconds if seconds is None else seconds
    _log_usage("settled", source=reservation.source, seconds=final, reservation_id=reservation.reservation_id)
    if db is None or not reservation.metered or not reservation.reservation_id:
        return
    try:
        await asyncio.to_thread(
            lambda: db.rpc(
                "settle_azure_seconds",
                {"p_reservation_id": reservation.reservation_id, "p_seconds": final},
            ).execute()
        )
    except Exception as e:
        log.warning("azure_budget: settle_azure_seconds failed (row stays reserved): %s", e)


async def release_azure_seconds(db, reservation: AzureReservation) -> None:
    """No billable call happened. Never raises — a failed release over-counts
    by this reservation, the safe direction for a budget."""
    _log_usage("released", source=reservation.source, seconds=reservation.seconds,
               reservation_id=reservation.reservation_id)
    if db is None or not reservation.metered or not reservation.reservation_id:
        return
    try:
        await asyncio.to_thread(
            lambda: db.rpc(
                "release_azure_seconds", {"p_reservation_id": reservation.reservation_id}
            ).execute()
        )
    except Exception as e:
        log.warning("azure_budget: release_azure_seconds failed (row stays reserved): %s", e)
