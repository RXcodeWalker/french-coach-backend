import asyncio
import json
import logging
import os
import re
from typing import Any, Dict

log = logging.getLogger("french-coach.scenario")

_SCENARIO_SYSTEM_PROMPT = """\
You are a French Language Learning Architect. Your goal is to take a user's description of a scenario and turn it into a structured, high-quality roleplay module.

The output must be a valid JSON object with the following fields:
1. "title": A catchy name for the scenario in English.
2. "scenario": A detailed description of the setup in English.
3. "npc_name": The name of the French-speaking character the user will interact with.
4. "npc_personality": A brief description of the NPC's character, tone, and behavior.
5. "objectives": A list of 3-5 specific tasks the user must accomplish in French (e.g., "Ask for the price", "Explain you are allergic").
6. "key_vocab": A list of 5-8 useful vocabulary items with "fr" (French) and "en" (English) translations.
7. "opening_line": The first sentence the NPC says to the user in French.

Scenario Design Rules:
- Keep the language level appropriate for a student (A2-B1 CEFR).
- The NPC should be helpful but might have specific constraints (e.g., a busy waiter, a strict guard).
- Objectives should encourage varied grammar (asking questions, describing things, giving opinions).

Example Output:
{
  "title": "Bakery Mishap",
  "scenario": "You are at a French bakery, but you realize you forgot your wallet after they've already packed your bread.",
  "npc_name": "Madame Lefebvre",
  "npc_personality": "A traditional but kind bakery owner who values her customers.",
  "objectives": [
    "Greet the owner",
    "Explain that you forgot your wallet",
    "Ask if you can pay later or by phone",
    "Apologize for the inconvenience"
  ],
  "key_vocab": [
    {"fr": "oublier", "en": "to forget"},
    {"fr": "le portefeuille", "en": "wallet"},
    {"fr": "déçu", "en": "disappointed"},
    {"fr": "régler", "en": "to pay/settle"}
  ],
  "opening_line": "Et voilà, deux baguettes et un croissant ! Ça fera quatre euros cinquante, s'il vous plaît."
}
"""

def extract_json(text: str) -> Dict[str, Any]:
    """Robustly extract JSON from AI response."""
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    text = text.strip()
    
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group()
    
    return json.loads(text)

async def generate_scenario(user_description: str) -> Dict[str, Any]:
    """Generates a structured roleplay scenario with multiple model fallbacks."""
    prompt = f"User Description: {user_description}\n\nGenerate the structured JSON scenario following the system instructions."
    
    # 1. Try Groq (Llama 3.3 70B) - Usually very fast and generous quota
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if groq_key:
        try:
            from groq import AsyncGroq
            client = AsyncGroq(api_key=groq_key)
            resp = await client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": _SCENARIO_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1000,
                temperature=0.7,
                response_format={"type": "json_object"}
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            log.warning(f"Scenario generation with Groq failed: {e}")

    # 2. Try Gemini 2.0 Flash
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel("gemini-2.0-flash", system_instruction=_SCENARIO_SYSTEM_PROMPT)
            response = await asyncio.to_thread(
                model.generate_content,
                prompt,
                generation_config={"response_mime_type": "application/json"}
            )
            return extract_json(response.text)
        except Exception as e:
            log.warning(f"Scenario generation with Gemini 2.0 failed: {e}")

    # 3. Last Resort: Gemini 1.5 Flash
    if gemini_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=_SCENARIO_SYSTEM_PROMPT)
            response = await asyncio.to_thread(model.generate_content, prompt)
            return extract_json(response.text)
        except Exception as e:
            log.error(f"Scenario generation with Gemini 1.5 failed: {e}")
            raise ValueError(f"All AI providers failed: {e}")

    raise ValueError("No AI provider API keys configured")
