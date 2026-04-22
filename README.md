# French AI Coach — Backend (phase 1)

FastAPI service that provides:

- **`POST /api/feedback`** — grades a French transcript via a free OpenRouter LLM and returns JSON in the shape `coach.js` expects.
- **`POST /api/transcribe`** — transcribes an uploaded audio blob using `faster-whisper` (French, with word timestamps). *Ready to use; frontend wiring lands in phase 2.*
- **`GET /health`** — liveness + config probe.

## Prerequisites

- Python 3.10+
- `ffmpeg` on PATH (required by faster-whisper for decoding webm/ogg/mp3)
  - Windows: `winget install Gyan.FFmpeg` or download from https://www.gyan.dev/ffmpeg/builds/
- A free OpenRouter API key: https://openrouter.ai/keys

## Setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
cp .env.example .env            # then edit .env, paste your OPENROUTER_API_KEY
```

## Run

```bash
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/health — you should see `"openrouter_configured": true`.

First request to `/api/transcribe` downloads the whisper model (`small` ~ 470 MB). Subsequent runs use the cached copy.

## Notes

- Free OpenRouter models are rate-limited. The backend tries a primary model, then a fallback; if both fail the frontend falls back to the local rule engine in `coach.js`.
- `CORS_ORIGINS=*` is fine for local dev. Lock it down before deploying.
- To swap whisper size, set `WHISPER_MODEL=base` (faster) or `medium` (more accurate) in `.env`.
