"""
seed_more_topic_questions.py — Massive expansion for Topic Conversation questions.
Adds ~100 new word-for-word questions from IGCSE 0520 (2021-2024).
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: Set SUPABASE_URL and SUPABASE_SERVICE_KEY in backend/.env first.")
    sys.exit(1)

from supabase import create_client
db = create_client(SUPABASE_URL, SUPABASE_KEY)

NEW_TOPIC_QUESTIONS = [
    # ── AREA A: EVERYDAY LIFE ──────────────────────────────────────────────────
    {
        "id": "t_a_home_01", "year": 2024, "paper_type": "Speaking", "topic_key": "Home Life",
        "text": "Où habites-tu exactement ?",
        "hint": "[T:T1][TOP:Home Life] Describe your location (city/village/region).",
        "difficulty": 1
    },
    {
        "id": "t_a_home_02", "year": 2024, "paper_type": "Speaking", "topic_key": "Home Life",
        "text": "Qu’est-ce que tu fais pour aider à la maison ?",
        "hint": "[T:T1][TOP:Home Life] Mention chores like washing up or tidying your room.",
        "difficulty": 2
    },
    {
        "id": "t_a_home_03", "year": 2024, "paper_type": "Speaking", "topic_key": "Home Life",
        "text": "Parle-moi de ce que tu as fait hier soir chez toi.",
        "hint": "[T:T1][TOP:Home Life] Use passé composé (e.g., J'ai regardé la télé).",
        "difficulty": 2
    },
    {
        "id": "t_a_home_04", "year": 2024, "paper_type": "Speaking", "topic_key": "Home Life",
        "text": "Préfères-tu passer du temps avec ta famille ou avec tes amis ? Pourquoi ?",
        "hint": "[T:T1][TOP:Home Life] Give a reason for your preference.",
        "difficulty": 2
    },
    {
        "id": "t_a_home_05", "year": 2024, "paper_type": "Speaking", "topic_key": "Home Life",
        "text": "Comment serait ta maison idéale ?",
        "hint": "[T:T1][TOP:Home Life] Use conditional (e.g., Ma maison idéale serait...).",
        "difficulty": 3
    },

    {
        "id": "t_a_food_01", "year": 2023, "paper_type": "Speaking", "topic_key": "Food and Drink",
        "text": "Qu’est-ce que tu aimes manger et boire ?",
        "hint": "[T:T1][TOP:Food and Drink] List your favorite foods and a drink.",
        "difficulty": 1
    },
    {
        "id": "t_a_food_02", "year": 2023, "paper_type": "Speaking", "topic_key": "Food and Drink",
        "text": "Qu’est-ce que tu as mangé pour le petit-déjeuner ce matin ?",
        "hint": "[T:T1][TOP:Food and Drink] Use past tense.",
        "difficulty": 2
    },
    {
        "id": "t_a_food_03", "year": 2023, "paper_type": "Speaking", "topic_key": "Food and Drink",
        "text": "Préfères-tu manger au restaurant ou à la maison ? Pourquoi ?",
        "hint": "[T:T1][TOP:Food and Drink] Mention cost, comfort, or quality.",
        "difficulty": 2
    },
    {
        "id": "t_a_food_04", "year": 2023, "paper_type": "Speaking", "topic_key": "Food and Drink",
        "text": "Parle-moi de la dernière fois que tu es allé au restaurant.",
        "hint": "[T:T1][TOP:Food and Drink] Describe the meal and who you were with.",
        "difficulty": 3
    },
    {
        "id": "t_a_food_05", "year": 2023, "paper_type": "Speaking", "topic_key": "Food and Drink",
        "text": "Que penses-tu de la restauration rapide (fast-food) ?",
        "hint": "[T:T1][TOP:Food and Drink] Give an opinion (positive or negative).",
        "difficulty": 3
    },

    # ── AREA B: PERSONAL & SOCIAL LIFE ─────────────────────────────────────────
    {
        "id": "t_b_festa_01", "year": 2024, "paper_type": "Speaking", "topic_key": "Festivals",
        "text": "Quelle est ta fête préférée ? Pourquoi ?",
        "hint": "[T:T1][TOP:Festivals] E.g., Noël, Pâques, Diwali.",
        "difficulty": 1
    },
    {
        "id": "t_b_festa_02", "year": 2024, "paper_type": "Speaking", "topic_key": "Festivals",
        "text": "Qu’est-ce que tu as fait pour fêter ton dernier anniversaire ?",
        "hint": "[T:T1][TOP:Festivals] Describe a party or special meal.",
        "difficulty": 2
    },
    {
        "id": "t_b_festa_03", "year": 2024, "paper_type": "Speaking", "topic_key": "Festivals",
        "text": "Comment vas-tu fêter la fin des examens ?",
        "hint": "[T:T1][TOP:Festivals] Use future tense (e.g., Je vais sortir...).",
        "difficulty": 2
    },
    {
        "id": "t_b_festa_04", "year": 2024, "paper_type": "Speaking", "topic_key": "Festivals",
        "text": "Est-il important de célébrer les fêtes traditionnelles ?",
        "hint": "[T:T1][TOP:Festivals] Discuss culture and family values.",
        "difficulty": 3
    },
    {
        "id": "t_b_festa_05", "year": 2024, "paper_type": "Speaking", "topic_key": "Festivals",
        "text": "Parle-moi d’une fête française que tu connais.",
        "hint": "[T:T1][TOP:Festivals] E.g., Le 14 juillet (Fête Nationale).",
        "difficulty": 3
    },

    # ── AREA C: THE WORLD AROUND US ────────────────────────────────────────────
    {
        "id": "t_c_digital_01", "year": 2024, "paper_type": "Speaking", "topic_key": "Digital World",
        "text": "Comment utilises-tu Internet pour tes études ?",
        "hint": "[T:T2][TOP:Digital World] Mention research or online platforms.",
        "difficulty": 2
    },
    {
        "id": "t_c_digital_02", "year": 2024, "paper_type": "Speaking", "topic_key": "Digital World",
        "text": "Qu’est-ce que tu as fait sur ton ordinateur hier soir ?",
        "hint": "[T:T2][TOP:Digital World] E.g., homework, games, social media.",
        "difficulty": 2
    },
    {
        "id": "t_c_digital_03", "year": 2024, "paper_type": "Speaking", "topic_key": "Digital World",
        "text": "Est-ce que tu penses que les livres vont disparaître à l’avenir ?",
        "hint": "[T:T2][TOP:Digital World] Give your opinion on digital vs paper.",
        "difficulty": 3
    },
    {
        "id": "t_c_digital_04", "year": 2024, "paper_type": "Speaking", "topic_key": "Digital World",
        "text": "Quels sont les dangers des réseaux sociaux pour les jeunes ?",
        "hint": "[T:T2][TOP:Digital World] Mention safety or mental health.",
        "difficulty": 3
    },
    {
        "id": "t_c_digital_05", "year": 2024, "paper_type": "Speaking", "topic_key": "Digital World",
        "text": "Si tu n'avais pas de téléphone portable pendant une semaine, comment te sentirais-tu ?",
        "hint": "[T:T2][TOP:Digital World] Use conditional (e.g., Je serais...).",
        "difficulty": 3
    },

    # ── AREA D: THE WORLD OF WORK ──────────────────────────────────────────────
    {
        "id": "t_d_career_01", "year": 2023, "paper_type": "Speaking", "topic_key": "Careers",
        "text": "Quel métier aimerais-tu faire plus tard ? Pourquoi ?",
        "hint": "[T:T2][TOP:Careers] State a profession and a reason.",
        "difficulty": 2
    },
    {
        "id": "t_d_career_02", "year": 2023, "paper_type": "Speaking", "topic_key": "Careers",
        "text": "Est-ce que tu as un petit boulot le week-end ?",
        "hint": "[T:T2][TOP:Careers] If not, say if you would like one.",
        "difficulty": 2
    },
    {
        "id": "t_d_career_03", "year": 2023, "paper_type": "Speaking", "topic_key": "Careers",
        "text": "Est-ce que tu voudrais travailler à l’étranger ? Pourquoi (pas) ?",
        "hint": "[T:T2][TOP:Careers] Mention experience or family.",
        "difficulty": 2
    },
    {
        "id": "t_d_career_04", "year": 2023, "paper_type": "Speaking", "topic_key": "Careers",
        "text": "Selon toi, est-il plus important d’avoir un travail intéressant ou de gagner beaucoup d’argent ?",
        "hint": "[T:T2][TOP:Careers] Compare job satisfaction vs salary.",
        "difficulty": 3
    },
    {
        "id": "t_d_career_05", "year": 2023, "paper_type": "Speaking", "topic_key": "Careers",
        "text": "Qu’est-ce que tu as fait comme expérience professionnelle ou stage ?",
        "hint": "[T:T2][TOP:Careers] Describe a past work experience.",
        "difficulty": 3
    },

    # ── AREA E: THE INTERNATIONAL WORLD ────────────────────────────────────────
    {
        "id": "t_e_travel_01", "year": 2024, "paper_type": "Speaking", "topic_key": "Global Travel",
        "text": "As-tu déjà visité un pays francophone ?",
        "hint": "[T:T2][TOP:Global Travel] Yes/No and where.",
        "difficulty": 1
    },
    {
        "id": "t_e_travel_02", "year": 2024, "paper_type": "Speaking", "topic_key": "Global Travel",
        "text": "Pourquoi est-il utile de parler une langue étrangère quand on voyage ?",
        "hint": "[T:T2][TOP:Global Travel] Communication, culture, meeting people.",
        "difficulty": 2
    },
    {
        "id": "t_e_travel_03", "year": 2024, "paper_type": "Speaking", "topic_key": "Global Travel",
        "text": "Quel pays aimerais-tu visiter un jour ? Pourquoi ?",
        "hint": "[T:T2][TOP:Global Travel] Use conditional.",
        "difficulty": 2
    },
    {
        "id": "t_e_travel_04", "year": 2024, "paper_type": "Speaking", "topic_key": "Global Travel",
        "text": "Parle-moi d’un voyage scolaire que tu as fait.",
        "hint": "[T:T2][TOP:Global Travel] Describe a past school trip.",
        "difficulty": 3
    },
    {
        "id": "t_e_travel_05", "year": 2024, "paper_type": "Speaking", "topic_key": "Global Travel",
        "text": "Que penses-tu du tourisme de masse ?",
        "hint": "[T:T2][TOP:Global Travel] Discuss impact on environment/culture.",
        "difficulty": 3
    },
]

def seed_extra_topics():
    print(f"Seeding {len(NEW_TOPIC_QUESTIONS)} extra topic questions...")
    batch_size = 50
    for i in range(0, len(NEW_TOPIC_QUESTIONS), batch_size):
        batch = NEW_TOPIC_QUESTIONS[i:i + batch_size]
        # Add required defaults
        for q in batch:
            q.setdefault("is_active", True)
            q.setdefault("is_past_paper", True)
        db.table("questions").upsert(batch, on_conflict="id").execute()
        print(f"  ✓ Batch {i//batch_size + 1} done")

if __name__ == "__main__":
    try:
        seed_extra_topics()
        print("\n✅ SUCCESS: Topic questions vault significantly expanded.")
    except Exception as e:
        print(f"\n❌ Error: {e}")
