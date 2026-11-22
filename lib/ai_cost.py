"""AI cost metering: one measured row per Groq / Gemini / Whisper call.

Why this exists: Premium is only a business if AI cost per active user is known
and below what a subscriber pays (migration 20261122090000, GET
/api/admin/ai-cost). This module records raw units (tokens, audio seconds) in
`ai_usage_events`; dollars are applied at read time from `ai_model_pricing`.

How a call is metered without touching ~20 call sites:

  * ``install_usage_hooks()`` (called once at startup from main.py) wraps the
    SDK methods every provider call goes through — Groq's
    ``AsyncCompletions.create`` and ``AsyncTranscriptions.create`` (so the three
    separately-built AsyncGroq clients are all covered) and Gemini's
    ``GenerativeModel.generate_content``. A new call site is metered for free;
    a new *provider* must add a hook here or its spend is invisible.
  * *Who* made the call comes from a ContextVar that
    ``lib/ai_quota.consume_ai_quota_or_503`` sets the moment a user is charged
    (it already holds the user id and feature). ``asyncio.to_thread`` and
    ``create_task`` copy the context, so the Gemini thread calls and the Groq
    stream task see it. If one request charges two features the last one wins
    for the feature label; the user is always right. Calls made with no charge
    bound (health probes, unmetered routes) are recorded as 'unattributed'.

Failure posture: metering must never break or slow a request. Rows go onto a
bounded in-memory queue and a daemon thread batch-inserts them; any error is
logged at WARNING and the row is dropped. A call whose response carries no
usage figure is still recorded (``measured=false``) so the gap is visible
rather than silently costed at zero. Nothing but ids and counts is stored — no
prompt, transcript or audio.

Off with ``AI_COST_TRACKING=0``.
"""

from __future__ import annotations

import contextvars
import logging
import os
import queue
import threading
from typing import Any, Callable

from lib.guest import is_guest

log = logging.getLogger("uvicorn.error")

UNATTRIBUTED = "unattributed"
_MAX_QUEUE = 5000
_BATCH = 50
_FLUSH_SECONDS = 3.0

# (user_id | None, is_guest, feature)
_ctx: contextvars.ContextVar[tuple[str | None, bool, str]] = contextvars.ContextVar(
    "ai_cost_ctx", default=(None, False, UNATTRIBUTED)
)


def enabled() -> bool:
    return os.getenv("AI_COST_TRACKING", "1").strip().lower() not in ("0", "false", "off", "no")


def bind_usage(user_id: str | None, feature: str) -> None:
    """Attribute the provider calls that follow, in this request, to `user_id`."""
    if not user_id:
        _ctx.set((None, False, feature or UNATTRIBUTED))
    elif is_guest(user_id):
        _ctx.set((None, True, feature or UNATTRIBUTED))
    else:
        _ctx.set((user_id, False, feature or UNATTRIBUTED))


# ── Sink ─────────────────────────────────────────────────────────────────────

_get_db: Callable[[], Any] | None = None
_q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=_MAX_QUEUE)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def configure(get_db: Callable[[], Any]) -> None:
    """Give the sink the service-role Supabase client getter (main.get_supabase)."""
    global _get_db
    _get_db = get_db


def _flush(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    try:
        db = _get_db() if _get_db else None
        if db is None:
            return
        db.table("ai_usage_events").insert(rows).execute()
    except Exception as exc:  # never let metering raise
        log.warning("ai_cost: dropped %d usage row(s): %s", len(rows), exc)


def _run_worker() -> None:
    while True:
        rows: list[dict[str, Any]] = []
        try:
            rows.append(_q.get(timeout=_FLUSH_SECONDS))
        except queue.Empty:
            continue
        while len(rows) < _BATCH:
            try:
                rows.append(_q.get_nowait())
            except queue.Empty:
                break
        _flush(rows)


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_worker, name="ai-cost-writer", daemon=True)
            _worker.start()


def _emit(
    *,
    provider: str,
    model: str,
    kind: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    audio_seconds: float = 0.0,
    measured: bool = True,
) -> None:
    if not enabled() or _get_db is None:
        return
    try:
        user_id, guest, feature = _ctx.get()
        row = {
            "user_id": user_id,
            "is_guest": guest,
            "feature": feature,
            "provider": provider,
            "model": model,
            "kind": kind,
            "input_tokens": max(int(input_tokens), 0),
            "output_tokens": max(int(output_tokens), 0),
            "audio_seconds": round(max(float(audio_seconds), 0.0), 3),
            "measured": measured,
        }
        _ensure_worker()
        _q.put_nowait(row)
    except queue.Full:
        log.warning("ai_cost: usage queue full, dropping a row")
    except Exception as exc:
        log.warning("ai_cost: could not record usage: %s", exc)


# ── Usage extraction (pure; tested without either SDK installed) ──────────────

def _num(obj: Any, name: str) -> int | None:
    v = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def groq_chat_tokens(usage: Any) -> tuple[int, int] | None:
    """(prompt, completion) tokens from a Groq usage object, or None if absent.
    Groq counts reasoning tokens inside completion_tokens."""
    if usage is None:
        return None
    p, c = _num(usage, "prompt_tokens"), _num(usage, "completion_tokens")
    if p is None and c is None:
        return None
    return p or 0, c or 0


def gemini_tokens(usage: Any) -> tuple[int, int] | None:
    """(prompt, output) tokens from Gemini's usage_metadata. Thinking tokens are
    billed as output, so they are added to the candidates count."""
    if usage is None:
        return None
    p, c = _num(usage, "prompt_token_count"), _num(usage, "candidates_token_count")
    t = _num(usage, "thoughts_token_count")
    if p is None and c is None:
        return None
    return p or 0, (c or 0) + (t or 0)


def whisper_seconds(transcription: Any) -> float | None:
    """Audio length of a verbose_json transcription: its `duration`, else the end
    of the last segment."""
    d = transcription.get("duration") if isinstance(transcription, dict) else getattr(transcription, "duration", None)
    if isinstance(d, (int, float)) and d > 0:
        return float(d)
    segs = transcription.get("segments") if isinstance(transcription, dict) else getattr(transcription, "segments", None)
    if segs:
        last = segs[-1]
        end = last.get("end") if isinstance(last, dict) else getattr(last, "end", None)
        if isinstance(end, (int, float)) and end > 0:
            return float(end)
    return None


def _chunk_usage(chunk: Any) -> Any:
    """A streamed Groq chunk carries usage on its last chunk, as `usage` or
    under the vendor `x_groq` field depending on SDK version."""
    u = getattr(chunk, "usage", None)
    if u is not None:
        return u
    xg = getattr(chunk, "x_groq", None)
    return getattr(xg, "usage", None) if xg is not None else None


# ── Hooks ────────────────────────────────────────────────────────────────────

def _record_groq_chat(model: str, usage: Any) -> None:
    tokens = groq_chat_tokens(usage)
    if tokens is None:
        _emit(provider="groq", model=model, kind="chat", measured=False)
    else:
        _emit(provider="groq", model=model, kind="chat", input_tokens=tokens[0], output_tokens=tokens[1])


class _MeteredStream:
    """Async-iterates a Groq stream unchanged and records usage when it ends
    (or is abandoned). Anything else is delegated to the real stream."""

    def __init__(self, inner: Any, model: str) -> None:
        self._inner = inner
        self._model = model
        self._usage: Any = None
        self._done = False
        self._it: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _finish(self) -> None:
        if not self._done:
            self._done = True
            _record_groq_chat(self._model, self._usage)

    def __aiter__(self) -> "_MeteredStream":
        return self

    async def __anext__(self) -> Any:
        try:
            if self._it is None:
                self._it = self._inner.__aiter__()
            chunk = await self._it.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException:
            self._finish()
            raise
        u = _chunk_usage(chunk)
        if u is not None:
            self._usage = u
        return chunk

    def __del__(self) -> None:  # abandoned mid-stream
        try:
            self._finish()
        except Exception:
            pass


def _wrap_groq_chat(orig: Callable[..., Any]) -> Callable[..., Any]:
    async def create(self: Any, *args: Any, **kwargs: Any) -> Any:
        resp = await orig(self, *args, **kwargs)
        model = str(kwargs.get("model") or getattr(resp, "model", "") or "unknown")
        if kwargs.get("stream"):
            return _MeteredStream(resp, model)
        _record_groq_chat(model, getattr(resp, "usage", None))
        return resp

    create._ai_cost_wrapped = True  # type: ignore[attr-defined]
    return create


def _wrap_groq_audio(orig: Callable[..., Any]) -> Callable[..., Any]:
    async def create(self: Any, *args: Any, **kwargs: Any) -> Any:
        resp = await orig(self, *args, **kwargs)
        model = str(kwargs.get("model") or "unknown")
        secs = whisper_seconds(resp)
        if secs is None:
            _emit(provider="groq", model=model, kind="audio", measured=False)
        else:
            _emit(provider="groq", model=model, kind="audio", audio_seconds=secs)
        return resp

    create._ai_cost_wrapped = True  # type: ignore[attr-defined]
    return create


def _wrap_gemini(orig: Callable[..., Any]) -> Callable[..., Any]:
    def generate_content(self: Any, *args: Any, **kwargs: Any) -> Any:
        resp = orig(self, *args, **kwargs)
        # A streamed response has no usage until consumed; those calls are not
        # made today (all use stream=False) and would show as unmeasured.
        model = str(getattr(self, "model_name", "") or "unknown").removeprefix("models/")
        tokens = gemini_tokens(getattr(resp, "usage_metadata", None))
        if tokens is None:
            _emit(provider="gemini", model=model, kind="chat", measured=False)
        else:
            _emit(provider="gemini", model=model, kind="chat", input_tokens=tokens[0], output_tokens=tokens[1])
        return resp

    generate_content._ai_cost_wrapped = True  # type: ignore[attr-defined]
    return generate_content


def _patch(owner: Any, name: str, wrapper: Callable[[Callable[..., Any]], Callable[..., Any]]) -> bool:
    orig = getattr(owner, name, None)
    if orig is None or getattr(orig, "_ai_cost_wrapped", False):
        return False
    setattr(owner, name, wrapper(orig))
    return True


def install_usage_hooks() -> list[str]:
    """Wrap the provider SDK entry points. Idempotent; a provider whose SDK is
    not installed is skipped. Returns the hooks installed this call."""
    if not enabled():
        return []
    done: list[str] = []
    try:
        from groq.resources.chat.completions import AsyncCompletions

        if _patch(AsyncCompletions, "create", _wrap_groq_chat):
            done.append("groq.chat")
    except ImportError:
        pass
    try:
        from groq.resources.audio.transcriptions import AsyncTranscriptions

        if _patch(AsyncTranscriptions, "create", _wrap_groq_audio):
            done.append("groq.audio")
    except ImportError:
        pass
    try:
        import google.generativeai as genai

        if _patch(genai.GenerativeModel, "generate_content", _wrap_gemini):
            done.append("gemini")
    except ImportError:
        pass
    return done
