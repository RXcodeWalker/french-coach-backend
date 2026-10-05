"""PCM WAV helpers for the exam pronunciation route (exam-pronunciation plan,
Batch 4): read the clip's parameters and cut it into the per-chunk WAVs that
Azure's 30 s REST short-audio limit requires.

The client normalises every exam turn to 16 kHz mono 16-bit WAV
(src/domain/pronunciation/audioNormalizer.ts) before upload, so plain PCM is
all this has to handle.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass


@dataclass(frozen=True)
class PcmWav:
    sample_rate: int
    channels: int
    sample_width: int
    frames: bytes

    @property
    def frame_count(self) -> int:
        return len(self.frames) // (self.channels * self.sample_width)

    @property
    def duration_s(self) -> float:
        return self.frame_count / self.sample_rate if self.sample_rate else 0.0


def read_pcm_wav(raw: bytes) -> PcmWav | None:
    """The clip's PCM parameters and frames, or None when `raw` is not a
    readable PCM WAV."""
    try:
        with wave.open(io.BytesIO(raw), "rb") as w:
            params = w.getparams()
            frames = w.readframes(params.nframes)
    except (wave.Error, EOFError, ValueError):
        return None
    if params.framerate <= 0 or params.nchannels <= 0 or params.sampwidth <= 0:
        return None
    return PcmWav(params.framerate, params.nchannels, params.sampwidth, frames)


def slice_wav(pcm: PcmWav, start_s: float, end_s: float) -> bytes:
    """A standalone WAV holding [start_s, end_s) of `pcm`."""
    frame_bytes = pcm.channels * pcm.sample_width
    start = max(0, min(pcm.frame_count, int(round(start_s * pcm.sample_rate))))
    end = max(start, min(pcm.frame_count, int(round(end_s * pcm.sample_rate))))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(pcm.channels)
        w.setsampwidth(pcm.sample_width)
        w.setframerate(pcm.sample_rate)
        w.writeframes(pcm.frames[start * frame_bytes:end * frame_bytes])
    return out.getvalue()
