@echo off
REM Quick-start for Windows. Run from the backend/ folder.
if not exist .venv (
    python -m venv .venv
    call .venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)
if not exist .env (
    copy .env.example .env
    echo.
    echo Created .env from template. Edit it to add your OPENROUTER_API_KEY, then re-run start.bat.
    exit /b 1
)
uvicorn main:app --reload --port 8000
