"""Every Groq/Gemini caller shares lib/model_config.py's model IDs.

Regression: exam_controller.py and scenario_generator.py kept their own
llama-3.3-70b-versatile / gemini-2.0-flash fallbacks after main.py moved on,
so /api/exam/interpret 404'd (model_not_found) on every call in production.
"""
import pathlib
import re

import exam_controller
import main
import scenario_generator
from lib import model_config

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_all_modules_use_the_shared_model_ids():
    for mod in (main, exam_controller, scenario_generator):
        assert mod.GROQ_MODEL == model_config.GROQ_MODEL
        assert mod.GEMINI_MODEL == model_config.GEMINI_MODEL


def test_no_live_module_hardcodes_its_own_model_default():
    # evaluator_service.py is the legacy, unreached scorer (ADR 0003) — out of scope.
    for name in ("main.py", "exam_controller.py", "scenario_generator.py"):
        src = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r'os\.getenv\(\s*"(GROQ|GEMINI)_MODEL"', src), name
        assert "llama-3.3-70b-versatile\"" not in src.replace("# ", ""), name


def test_interpret_reserves_tokens_for_reasoning():
    if model_config.GROQ_REASONING_EFFORT:
        assert model_config.groq_token_budget(60) > 60
        assert model_config.groq_reasoning_kwargs() == {"reasoning_effort": model_config.GROQ_REASONING_EFFORT}
