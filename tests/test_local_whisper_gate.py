"""Local faster-whisper must never load unless explicitly enabled.

get_whisper() pulls a multi-hundred-MB model into the worker process. On a
512MB host the kernel OOM-kills the worker mid-load; that kill is uncatchable
and reaches the browser as a bare 502 with an empty body. The gate therefore
lives inside get_whisper() itself rather than at each call site, so that a new
caller cannot reintroduce the crash by forgetting to check the flag.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _reload_main(monkeypatch, **env):
    for key in ("LOCAL_WHISPER_ENABLED", "PRONUNCIATION_LOCAL_WHISPER"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("main", None)
    return importlib.import_module("main")


def test_local_whisper_is_off_by_default(monkeypatch):
    main = _reload_main(monkeypatch)
    assert main.LOCAL_WHISPER_ENABLED is False


@pytest.mark.parametrize(
    "env",
    [
        {"LOCAL_WHISPER_ENABLED": "1"},
        {"LOCAL_WHISPER_ENABLED": "true"},
        # Older endpoint-scoped spelling, still honoured so deployed configs
        # that set it keep working.
        {"PRONUNCIATION_LOCAL_WHISPER": "1"},
    ],
)
def test_opt_in_spellings(monkeypatch, env):
    main = _reload_main(monkeypatch, **env)
    assert main.LOCAL_WHISPER_ENABLED is True


def test_get_whisper_refuses_to_load_when_disabled(monkeypatch):
    """The important half: raising here is what keeps the model out of memory.
    A RuntimeError is catchable by callers; an OOM kill is not."""
    main = _reload_main(monkeypatch)

    def _fail_import(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("faster_whisper was imported despite the gate")

    monkeypatch.setitem(sys.modules, "faster_whisper", _fail_import)

    with pytest.raises(RuntimeError, match="LOCAL_WHISPER_ENABLED"):
        main.get_whisper()


def test_pronunciation_router_gets_no_local_fallback_when_disabled(monkeypatch):
    """main.py wires the router's faster-whisper seam to None while the flag is
    off, so /api/pronunciation degrades to couldNotAssess instead of loading a
    model (covered end to end in test_pronunciation.py)."""
    main = _reload_main(monkeypatch)
    import routers.pronunciation as pron

    assert main.LOCAL_WHISPER_ENABLED is False
    assert pron._faster_whisper_fn is None
