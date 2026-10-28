"""Where a recorded French line lives in the public `tts-audio` bucket.

A port of the main repo's `src/services/tts/voiceKey.ts` (recorded-voices plan
§3, §4.1). The app plays a line from the path that module computes, and the
"Record voices" script writes to it; POST /api/native-voice
(routers/native_voice.py) caches an AI-corrected sentence under the same path,
so a sentence it caches and a sentence the script records are one file. If the
two ever disagreed the cached file would simply never be found (the app falls
back to the browser voice and nothing errors), so the format is pinned by the
vectors in `data/tts/voice_key_vectors.json`, which both repos' tests check.

Change nothing here without the matching change in voiceKey.ts and a
VOICE_KEY_VERSION bump (which re-records everything).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

VOICE_KEY_VERSION = "v2"
RECORDED_VOICES = {"henri": "fr-FR-HenriNeural"}
DEFAULT_VOICE = "henri"
TTS_BUCKET = "tts-audio"

# JavaScript's `\s` exactly (ECMAScript WhiteSpace + LineTerminator). Python's
# own `\s` differs: it also matches \x1c-\x1f and misses U+FEFF, so it would
# key some texts differently from the app.
_JS_WHITESPACE = re.compile(
    "[\t\n\u000b\u000c\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)


def normalize_spoken_text(text: str) -> str:
    """NFC, every whitespace run as one space, trimmed. Apostrophes and
    punctuation are left alone (they change what Azure says)."""
    # After the substitution every whitespace run is one plain space, so trimming spaces is a full trim.
    return _JS_WHITESPACE.sub(" ", unicodedata.normalize("NFC", text)).strip(" ")


def voice_object_path(text: str, voice: str = DEFAULT_VOICE) -> str:
    """The object path inside TTS_BUCKET for this line in this voice."""
    if voice not in RECORDED_VOICES:
        raise ValueError(f"unknown recorded voice: {voice!r}")
    digest = hashlib.sha256(f"{VOICE_KEY_VERSION}|{voice}|{normalize_spoken_text(text)}".encode("utf-8")).hexdigest()
    return f"{VOICE_KEY_VERSION}/{voice}/{digest[:32]}.mp3"
