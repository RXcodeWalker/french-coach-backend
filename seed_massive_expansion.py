"""
seed_massive_expansion.py — Import 110+ new questions from massive_expansion.json into Supabase.
"""

import os
import sys
import json
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: Set SUPABASE_URL and SUPABASE_SERVICE_KEY in backend/.env first.")
    sys.exit(1)

from supabase import create_client
db = create_client(SUPABASE_URL, SUPABASE_KEY)

def seed_expansion():
    file_path = os.path.join(os.path.dirname(__file__), 'massive_expansion.json')
    with open(file_path, 'r', encoding='utf-8') as f:
        questions = json.load(f)

    print(f"Seeding {len(questions)} new questions...")
    batch_size = 20
    for i in range(0, len(questions), batch_size):
        batch = questions[i:i + batch_size]
        # Ensure required defaults
        for q in batch:
            q.setdefault("follow_ups", [])
            q.setdefault("key_vocab", [])
            q.setdefault("is_active", True)
            q.setdefault("is_past_paper", False)
            q.setdefault("model_answer", "")
            # Map topic_key if needed (they should already be correct)
        
        try:
            db.table("questions").upsert(batch, on_conflict="id").execute()
            print(f"  ✓ Questions {i + 1}–{min(i + batch_size, len(questions))} done")
        except Exception as e:
            print(f"  ❌ Error seeding batch {i}: {e}")

if __name__ == "__main__":
    seed_expansion()
    print("\n✅ Done! Database expanded with massive question set.")
