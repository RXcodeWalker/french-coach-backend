"""Phase 3 Batch C: the coach prompts must not invent Cambridge scoring.

No `igcseLevel` band label, no hand-rolled accuracy "subtract" formula, no
"Extended —" tier names, and none of the "earns marks" / "one band higher"
claims the Teachers' Notes don't support.
"""
import main

PROMPTS = {
    "SYSTEM_PROMPT": main.SYSTEM_PROMPT,
    "MULTIMODAL_SYSTEM_PROMPT": main.MULTIMODAL_SYSTEM_PROMPT,
}

BANNED = ["igcseLevel", "subtract", "Extended —", "Core — Secure", "earn IGCSE marks", "directly earns marks", "IGCSE band higher"]


def test_coach_prompts_contain_no_invented_scoring():
    for name, prompt in PROMPTS.items():
        for phrase in BANNED:
            assert phrase not in prompt, f"{name} still contains {phrase!r}"


def test_accuracy_is_a_holistic_practice_judgement_not_a_formula():
    assert "holistic practice judgement of accuracy" in main.SYSTEM_PROMPT
    assert "not a Cambridge mark" in main.SYSTEM_PROMPT
