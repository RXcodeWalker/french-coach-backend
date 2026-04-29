"""
seed_speaking_vault.py — Complete IGCSE French 0520 Speaking Vault.
Fitted for MINIMAL 'questions' table schema.
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

# Data from previous extraction
EXAM_SETS = [
    {
        "session_id": "v_24_mj_c1", "year": 2024, "session": "May/June", "variant": "1",
        "role_play_scenario": "Pendant les vacances, vous trouvez un job à l’office de tourisme de votre ville. Vous parlez de votre job avec un(e) ami(e) français(e).",
        "role_play_tasks": [
            {"q": "Bonjour ! C’est super que tu aies un job ! Où est-ce que tu travailles exactement ?", "hint": "Tell your friend where the office is."},
            {"q": "Et qu’est-ce que tu fais comme travail là-bas ?", "hint": "Describe 1-2 tasks you do."},
            {"q": "C’est intéressant ! À quelle heure est-ce que tu commences le matin ?", "hint": "Give a specific time."},
            {"q": "Qu’est-ce que tu as fait au travail hier ?", "hint": "Use passé composé to describe an event."},
            {"q": "Est-ce que tu voudrais travailler dans le tourisme à l’avenir ? Pourquoi (pas) ?", "hint": "Give a reason (future/conditional)."}
        ],
        "topic_1": {"name": "Area A: Daily Routine", "questions": ["À quelle heure est-ce que tu te lèves ?", "Qu'est-ce que tu fais pour aider à la maison ?", "Décris ta routine le soir.", "Qu'as-tu fait hier ?", "Que changerais-tu à ta routine ?"]},
        "topic_2": {"name": "Area D: Education", "questions": ["Où se trouve ton école ?", "Quelles matières préfères-tu ?", "Pourquoi ?", "Qu'as-tu fait hier à l'école ?", "Projets après les examens ?"]}
    },
    {
        "session_id": "v_24_mj_c4", "year": 2024, "session": "May/June", "variant": "2",
        "role_play_scenario": "Vous êtes à la gare à Paris. Vous voulez acheter des billets de train pour aller à Lyon.",
        "role_play_tasks": [
            {"q": "Bonjour. Je peux vous aider ?", "hint": "Say you want to buy 2 tickets for Lyon."},
            {"q": "C'est pour quel jour ?", "hint": "Give a day or date."},
            {"q": "Vous préférez voyager le matin ou l'après-midi ?", "hint": "Pick an option."},
            {"q": "Pourquoi allez-vous à Lyon ?", "hint": "Give a reason (e.g., visiting family)."},
            {"q": "Qu'est-ce que vous allez faire à Lyon pendant votre séjour ?", "hint": "Describe 1-2 plans (future)."}
        ],
        "topic_1": {"name": "Area B: Holidays", "questions": ["Où vas-tu en vacances ?", "Activités préférées ?", "Dernier voyage ?", "Prochain voyage ?", "Importance des vacances ?"]},
        "topic_2": {"name": "Area D: Careers", "questions": ["Job de tes rêves ?", "Pourquoi ?", "Stage en entreprise ?", "Qualités nécessaires ?", "Travailler à l'étranger ?"]}
    },
    {
        "session_id": "v_24_mj_c9", "year": 2024, "session": "May/June", "variant": "3",
        "role_play_scenario": "Vous êtes au marché en France. Vous voulez acheter des fruits.",
        "role_play_tasks": [
            {"q": "Bonjour. Qu'est-ce que vous désirez ?", "hint": "Say you want 1 kilo of apples."},
            {"q": "Et avec ceci ?", "hint": "Add another fruit or vegetable."},
            {"q": "C'est pour manger aujourd'hui ou demain ?", "hint": "Pick an option."},
            {"q": "Pourquoi aimez-vous les fruits ?", "hint": "Give a reason (e.g., healthy)."},
            {"q": "Qu'est-ce que vous allez cuisiner ce soir ?", "hint": "Mention a dish (future)."}
        ],
        "topic_1": {"name": "Area B: Family/Friends", "questions": ["Décris ta famille.", "Meilleur ami ?", "Sorties avec amis ?", "Importance de l'amitié ?", "Conflits familiaux ?"]},
        "topic_2": {"name": "Area C: Environment", "questions": ["Problèmes écolo ?", "Recyclage ?", "Transport écolo ?", "Changement climatique ?", "Futur de la planète ?"]}
    },
    {
        "session_id": "v_23_mj_c1", "year": 2023, "session": "May/June", "variant": "1",
        "role_play_scenario": "Vous organisez des vacances au camping en France avec un(e) ami(e) français(e).",
        "role_play_tasks": [
            {"q": "Salut ! Où est-ce qu'on va faire du camping exactement ?", "hint": "Choose a region (e.g., Bretagne)."},
            {"q": "On part quand ?", "hint": "Give a month or date."},
            {"q": "Tu préfères dormir dans une tente ou dans une caravane ?", "hint": "Choose one."},
            {"q": "Qu'est-ce que tu as acheté pour le voyage ?", "hint": "Mention 1-2 items bought (passé composé)."},
            {"q": "Pourquoi aimes-tu faire du camping ?", "hint": "Give a reason."}
        ],
        "topic_1": {"name": "Area A: Food/Health", "questions": ["Manger sain ?", "Plat préféré ?", "Hier soir ?", "Sport pour santé ?", "Fast food ?"]},
        "topic_2": {"name": "Area C: Town/Region", "questions": ["Où habites-tu ?", "Avantages de ta ville ?", "Changements ?", "Week-end dernier ?", "Vivre à la campagne ?"]}
    },
    {
        "session_id": "v_23_on_c1", "year": 2023, "session": "Oct/Nov", "variant": "1",
        "role_play_scenario": "Vous êtes à la patinoire avec un(e) ami(e) français(e).",
        "role_play_tasks": [
            {"q": "C'est super d'être ici ! Tu sais bien patiner ?", "hint": "Say how good you are at skating."},
            {"q": "On reste combien de temps ?", "hint": "Suggest a duration."},
            {"q": "Tu veux un chocolat chaud ou un jus d'orange après ?", "hint": "Pick a drink."},
            {"q": "Qu'est-ce que tu as fait comme sport le week-end dernier ?", "hint": "Mention a sport (passé composé)."},
            {"q": "Quel nouveau sport voudrais-tu essayer ?", "hint": "Name a sport and why (future/conditional)."}
        ],
        "topic_1": {"name": "Area B: Festivals", "questions": ["Fête préférée ?", "Dernier anniversaire ?", "Cadeaux ?", "Importance des fêtes ?", "Traditions ?"]},
        "topic_2": {"name": "Area E: Technology", "questions": ["Usage d'internet ?", "Réseaux sociaux ?", "Avantages/Inconvénients ?", "Hier en ligne ?", "IA dans le futur ?"]}
    },
    {
        "session_id": "v_22_mj_c1", "year": 2022, "session": "May/June", "variant": "1",
        "role_play_scenario": "Vous êtes au buffet de la gare. Vous voulez acheter quelque chose à manger.",
        "role_play_tasks": [
            {"q": "Bonjour. Qu'est-ce que vous voulez commander ?", "hint": "Order a ham sandwich."},
            {"q": "Et comme boisson ?", "hint": "Choose a drink."},
            {"q": "C'est pour manger ici ou pour emporter ?", "hint": "Choose one."},
            {"q": "Pourquoi voyagez-vous en train aujourd'hui ?", "hint": "Give a reason."},
            {"q": "Où allez-vous passer la nuit ?", "hint": "Mention a hotel or friend's house (future)."}
        ],
        "topic_1": {"name": "Area A: Shopping", "questions": ["Où fais-tu du shopping ?", "Vêtements préférés ?", "Argent de poche ?", "Dernier achat ?", "Shopping en ligne ?"]},
        "topic_2": {"name": "Area E: World Issues", "questions": ["Problème mondial ?", "Pauvreté ?", "Conflits ?", "Ton rôle ?", "Espoir pour futur ?"]}
    },
    {
        "session_id": "v_22_mj_c3", "year": 2022, "session": "May/June", "variant": "2",
        "role_play_scenario": "Vous téléphonez à un hôtel en Suisse pour demander un job d'été.",
        "role_play_tasks": [
            {"q": "Hôtel de la Paix, bonjour. Je peux vous aider ?", "hint": "Say you're calling about a summer job."},
            {"q": "Quel âge avez-vous ?", "hint": "State your age."},
            {"q": "Vous parlez quelles langues ?", "hint": "Mention 2 languages."},
            {"q": "Pourquoi voulez-vous travailler chez nous ?", "hint": "Give a reason."},
            {"q": "Quand pouvez-vous commencer ?", "hint": "Give a date/month."}
        ],
        "topic_1": {"name": "Area B: Personal Life", "questions": ["Où habites-tu ?", "Maison/Appartement ?", "Ta chambre ?", "Changements ?", "Vivre ailleurs ?"]},
        "topic_2": {"name": "Area D: Future Plans", "questions": ["Université ?", "Pourquoi ?", "Année sabbatique ?", "Vivre à l'étranger ?", "Mariage/Enfants ?"]}
    },
    {
        "session_id": "v_21_on_c1", "year": 2021, "session": "Oct/Nov", "variant": "1",
        "role_play_scenario": "Vous êtes au cinéma avec un(e) ami(e) belge. Vous choisissez un film.",
        "role_play_tasks": [
            {"q": "Salut ! Quel genre de film tu veux voir ?", "hint": "Pick a genre (e.g., comédie)."},
            {"q": "On prend la séance de 18h ou de 20h ?", "hint": "Pick one."},
            {"q": "Tu veux du popcorn ?", "hint": "Yes/No and quantity."},
            {"q": "Quel était le dernier film que tu as vu ?", "hint": "Mention title (passé composé)."},
            {"q": "Pourquoi aimes-tu aller au cinéma ?", "hint": "Give a reason."}
        ],
        "topic_1": {"name": "Area A: Daily Activities", "questions": ["Aide à la maison ?", "Cuisine ?", "Hier soir ?", "Manger sain ?", "Routine ?"]},
        "topic_2": {"name": "Area E: Travel", "questions": ["Voyage préféré ?", "France ?", "Langues ?", "Futur voyage ?", "Importance de voyager ?"]}
    },
    {
        "session_id": "v_24_fm_c4", "year": 2024, "session": "Feb/March", "variant": "1",
        "role_play_scenario": "Vous êtes à la gare routière. Vous achetez des billets de bus.",
        "role_play_tasks": [
            {"q": "Bonjour. Pour quelle destination ?", "hint": "Say you want to go to Nice."},
            {"q": "Aller simple ou aller-retour ?", "hint": "Pick one."},
            {"q": "Vous avez une carte de réduction ?", "hint": "Say yes or no."},
            {"q": "Pourquoi allez-vous à Nice ?", "hint": "Give a reason."},
            {"q": "Où allez-vous loger ?", "hint": "Mention hotel/friend (future)."}
        ],
        "topic_1": {"name": "Area B: Hobbies", "questions": ["Passe-temps ?", "Sport ?", "Temps libre ?", "Week-end dernier ?", "Nouveau hobby ?"]},
        "topic_2": {"name": "Area C: Local Customs", "questions": ["Fêtes locales ?", "Cuisine locale ?", "Vêtements traditionnels ?", "Hier fêté ?", "Importance culture ?"]}
    },
    {
        "session_id": "v_24_mj_c2", "year": 2024, "session": "May/June", "variant": "1",
        "role_play_scenario": "Vous allez à un concert de rock avec un(e) ami(e) suisse.",
        "role_play_tasks": [
            {"q": "C'est génial ! À quelle heure on se retrouve ?", "hint": "Suggest a meeting time."},
            {"q": "On y va en bus ou à pied ?", "hint": "Pick one."},
            {"q": "Tu connais bien ce groupe ?", "hint": "Explain your interest."},
            {"q": "Qu'est-ce que tu as fait comme activité musicale récemment ?", "hint": "Mention 1 activity (passé composé)."},
            {"q": "Quel autre concert voudrais-tu voir ?", "hint": "Future group/artist and why."}
        ],
        "topic_1": {"name": "Area A: Health", "questions": ["Sport ?", "Alimentation ?", "Sommeil ?", "Récemment ?", "Conseil santé ?"]},
        "topic_2": {"name": "Area D: School Life", "questions": ["Matières ?", "Profs ?", "Cantine ?", "Hier ?", "Futur ?"]}
    },
    {
        "session_id": "v_24_mj_c8", "year": 2024, "session": "May/June", "variant": "1",
        "role_play_scenario": "Vous avez perdu votre portable au camping. Vous parlez au/à la réceptionniste.",
        "role_play_tasks": [
            {"q": "Bonjour. Je peux vous aider ?", "hint": "Say you lost your phone."},
            {"q": "C'est quelle marque ?", "hint": "Give a brand (e.g., iPhone)."},
            {"q": "Où est-ce que vous l'avez vu pour la dernière fois ?", "hint": "Mention a place (e.g., swimming pool)."},
            {"q": "Pourquoi avez-vous besoin de votre téléphone maintenant ?", "hint": "Give a reason."},
            {"q": "Qu'est-ce que vous allez faire si on ne le trouve pas ?", "hint": "Describe your plan (future)."}
        ],
        "topic_1": {"name": "Area C: Environment", "questions": ["Nature ?", "Pollution ?", "Solutions ?", "Hier ?", "Planète 2050 ?"]},
        "topic_2": {"name": "Area E: Global Events", "questions": ["Actu ?", "Problème ?", "IA ?", "Hier vu ?", "Futur ?"]}
    },
    {
        "session_id": "v_23_mj_c5", "year": 2023, "session": "May/June", "variant": "1",
        "role_play_scenario": "Vous voulez louer un bateau en vacances en Martinique.",
        "role_play_tasks": [
            {"q": "Bonjour. Quel type de bateau voulez-vous ?", "hint": "Choose a small motor boat."},
            {"q": "C'est pour combien de temps ?", "hint": "Say 3 hours."},
            {"q": "Vous savez conduire un bateau ?", "hint": "Yes/No and experience."},
            {"q": "Où êtes-vous allé hier en vacances ?", "hint": "Mention a place (passé composé)."},
            {"q": "Qu'est-ce que vous allez faire demain ?", "hint": "Future plan."}
        ],
        "topic_1": {"name": "Area B: Occasions", "questions": ["Anniversaire ?", "Cadeaux ?", "Fête préférée ?", "Dernière fête ?", "Futur ?"]},
        "topic_2": {"name": "Area D: Careers", "questions": ["Job idéal ?", "Argent ?", "Stage ?", "Qualités ?", "Avenir ?"]}
    },
    {
        "session_id": "v_23_on_c3", "year": 2023, "session": "Oct/Nov", "variant": "1",
        "role_play_scenario": "Vous vous inscrivez à un nouveau club de sport.",
        "role_play_tasks": [
            {"q": "Bonjour. Quel sport voulez-vous faire ?", "hint": "Pick a sport (e.g., tennis)."},
            {"q": "Vous préférez jouer le matin ou le soir ?", "hint": "Pick one."},
            {"q": "Vous avez déjà fait ce sport ?", "hint": "Explain your level."},
            {"q": "Pourquoi voulez-vous rejoindre notre club ?", "hint": "Give a reason."},
            {"q": "Quand voulez-vous commencer ?", "hint": "Give a date (future)."}
        ],
        "topic_1": {"name": "Area A: Daily Routine", "questions": ["Lever ?", "Coucher ?", "Devoirs ?", "Détente hier ?", "Changement ?"]},
        "topic_2": {"name": "Area C: Town", "questions": ["Description ?", "Loisirs ?", "Transport ?", "Samedi dernier ?", "Futur ?"]}
    },
    {
        "session_id": "v_22_mj_c2", "year": 2022, "session": "May/June", "variant": "1",
        "role_play_scenario": "Vous retrouvez un(e) ami(e) à la gare. Vous discutez de vos projets.",
        "role_play_tasks": [
            {"q": "Salut ! Tu as fait bon voyage ?", "hint": "Say yes, it was long but fine."},
            {"q": "Tu as faim ?", "hint": "Say you want to eat something."},
            {"q": "On va au café ou directement à la maison ?", "hint": "Pick one."},
            {"q": "Qu'est-ce que tu as fait hier soir ?", "hint": "Passé composé activity."},
            {"q": "Qu'est-ce qu'on va faire demain ?", "hint": "Future plan."}
        ],
        "topic_1": {"name": "Area B: Holidays", "questions": ["Destinations ?", "Activités ?", "Logement ?", "Dernières vacances ?", "Rêve ?"]},
        "topic_2": {"name": "Area D: School", "questions": ["Matières ?", "Uniforme ?", "Règles ?", "Hier ?", "Université ?"]}
    },
    {
        "session_id": "v_21_sp_c1", "year": 2021, "session": "Specimen", "variant": "1",
        "role_play_scenario": "Vous êtes au zoo avec un(e) ami(e) français(e).",
        "role_play_tasks": [
            {"q": "Regarde ! Quel animal tu veux voir en premier ?", "hint": "Pick an animal (e.g., lion)."},
            {"q": "On prend le petit train ou on marche ?", "hint": "Pick one."},
            {"q": "Tu aimes les zoos ?", "hint": "Give your opinion."},
            {"q": "Qu'est-ce que tu as mangé à midi ?", "hint": "Mention food (passé composé)."},
            {"q": "Qu'est-ce que tu vas faire ce soir ?", "hint": "Future plan."}
        ],
        "topic_1": {"name": "Area B: Family", "questions": ["Description ?", "Rapports ?", "Week-end en famille ?", "Hier soir ?", "Futur ?"]},
        "topic_2": {"name": "Area C: Customs", "questions": ["Fêtes ?", "Plats ?", "Traditions ?", "Dernière fête ?", "Importance ?"]}
    }
]

def seed_to_questions_table():
    print(f"Preparing to seed {len(EXAM_SETS)} exam sets into 'questions' table...")
    all_questions = []

    for exam in EXAM_SETS:
        # 1. Add Role Play Tasks
        for idx, task in enumerate(exam["role_play_tasks"]):
            all_questions.append({
                "id": f"{exam['session_id']}_rp_{idx+1}",
                "paper_type": "Speaking",
                "is_past_paper": True,
                "year": exam["year"],
                "topic_key": "role_play",
                "text": task["q"],
                "hint": f"[S:{exam['session_id']}][SES:{exam['session']}][VAR:{exam['variant']}][T:RP][SCN:{exam['role_play_scenario']}] {task['hint']}",
                "difficulty": 2
            })

        # 2. Add Topic 1 Questions
        for idx, q_text in enumerate(exam["topic_1"]["questions"]):
            all_questions.append({
                "id": f"{exam['session_id']}_t1_{idx+1}",
                "paper_type": "Speaking",
                "is_past_paper": True,
                "year": exam["year"],
                "topic_key": exam["topic_1"]["name"],
                "text": q_text,
                "hint": f"[S:{exam['session_id']}][SES:{exam['session']}][VAR:{exam['variant']}][T:T1][TOP:{exam['topic_1']['name']}]",
                "difficulty": 2
            })

        # 3. Add Topic 2 Questions
        for idx, q_text in enumerate(exam["topic_2"]["questions"]):
            all_questions.append({
                "id": f"{exam['session_id']}_t2_{idx+1}",
                "paper_type": "Speaking",
                "is_past_paper": True,
                "year": exam["year"],
                "topic_key": exam["topic_2"]["name"],
                "text": q_text,
                "hint": f"[S:{exam['session_id']}][SES:{exam['session']}][VAR:{exam['variant']}][T:T2][TOP:{exam['topic_2']['name']}]",
                "difficulty": 3
            })

    print(f"Total questions generated: {len(all_questions)}")
    # Upsert in batches
    batch_size = 50
    for i in range(0, len(all_questions), batch_size):
        batch = all_questions[i:i + batch_size]
        # Only include columns we are sure exist
        db.table("questions").upsert(batch, on_conflict="id").execute()
        print(f"  ✓ Batch {i//batch_size + 1} done")

if __name__ == "__main__":
    try:
        seed_to_questions_table()
        print("\n✅ SUCCESS: 15 exam sets added with encoded hints.")
    except Exception as e:
        print(f"\n❌ Error: {e}")
