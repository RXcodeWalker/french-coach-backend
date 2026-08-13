# French AI Coach Backend API

FastAPI API service for the separate frontend application. This backend does not serve `index.html` or any frontend static assets.

## Endpoints

- `GET /` - API status JSON.
- `GET /health` - liveness and configuration probe.
- `POST /api/feedback`, `/api/feedback/v2`, `/api/feedback/v3` - speaking feedback with AI fallback.
- `POST /api/transcribe` - French speech-to-text via Groq Whisper or local faster-whisper.
- `GET /docs` and `GET /openapi.json` - FastAPI documentation.

## Local Setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload --port 8000
```

## Render

Use this startup command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

`.python-version` pins Render to Python 3.11.11 for compatibility with AI/audio dependencies.

## Environment

- `GROQ_API_KEY` and `GEMINI_API_KEY` are optional but recommended.
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, and `SUPABASE_JWT_SECRET` are required for database-backed endpoints.
- `CORS_ORIGINS` should be your frontend domains, comma-separated, with no trailing slash.
- `IGCSE_DB_PATH` defaults to `data/igcse_speaking.db`.
- `AZURE_SPEECH_KEY` and `AZURE_SPEECH_REGION` are optional and enable phoneme/prosody-level pronunciation assessment via Azure AI Speech. Get them from a "Speech" resource at [portal.azure.com](https://portal.azure.com) (the free F0 tier has no cost but has monthly-hour and rate quotas). Without both set, pronunciation scoring degrades gracefully to the `whisper-heuristic` tier (word-diff only, no acoustic signal) — no crash, no fabricated score.

Hosted AI providers are rate-limited. The backend logs failures, tries provider fallbacks, and returns structured offline JSON when providers are unavailable.
