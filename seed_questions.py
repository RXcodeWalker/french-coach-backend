"""
seed_questions.py — Populate Supabase with all questions, daily challenges, and exam sets.

Run once (safe to re-run — uses upsert):
  cd backend
  python seed_questions.py
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: Set SUPABASE_URL and SUPABASE_SERVICE_KEY in backend/.env first.")
    sys.exit(1)

from supabase import create_client
db = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── QUESTIONS ─────────────────────────────────────────────────────────────────
# Fields: id, topic_key, text, hint, model_answer, difficulty,
#         follow_ups (list), key_vocab (list of {fr, en}),
#         year, paper_type, paper_code, question_number, is_past_paper

QUESTIONS = [
    # ── L'ÉCOLE ──────────────────────────────────────────────────────────────
    {
        "id": "sch_01", "topic_key": "school", "difficulty": 1, "is_past_paper": False,
        "text": "Parle-moi de ton école.",
        "hint": "Talk about your school — size, subjects, teachers, uniform, facilities.",
        "model_answer": "Mon école s'appelle City Academy et se trouve en ville. C'est une grande école avec environ mille élèves. J'aime bien mon école parce que les professeurs sont sympathiques et il y a beaucoup d'activités parascolaires. Mes matières préférées sont les maths et les sciences. On porte un uniforme — un pantalon noir et un pull bleu marine — ce qui, à mon avis, est pratique.",
        "follow_ups": ["Quel est ton professeur préféré et pourquoi ?", "Combien d'élèves y a-t-il dans ton école ?", "Est-ce que tu portes un uniforme scolaire ?"],
        "key_vocab": [{"fr": "le/la proviseur(e)", "en": "headteacher"}, {"fr": "la cour de récréation", "en": "playground"}, {"fr": "la bibliothèque", "en": "library"}, {"fr": "les activités parascolaires", "en": "extracurricular activities"}],
    },
    {
        "id": "sch_02", "topic_key": "school", "difficulty": 1, "is_past_paper": False,
        "text": "Quelles sont tes matières préférées et pourquoi ?",
        "hint": "Describe 2-3 favourite subjects, give reasons, compare with subjects you dislike.",
        "model_answer": "Ma matière préférée, c'est le français parce que j'aime beaucoup les langues étrangères et je trouve que c'est utile pour voyager. J'aime aussi les sciences car les expériences sont fascinantes. Par contre, je n'aime pas trop l'EPS parce que je ne suis pas très sportif(ve).",
        "follow_ups": ["À quelle heure commencent tes cours ?", "Est-ce que les cours sont difficiles pour toi ?", "Qu'est-ce que tu fais pendant la récréation ?"],
        "key_vocab": [{"fr": "utile/inutile", "en": "useful/useless"}, {"fr": "fascinant(e)", "en": "fascinating"}, {"fr": "par contre", "en": "on the other hand"}],
    },
    {
        "id": "sch_03", "topic_key": "school", "difficulty": 1, "is_past_paper": False,
        "text": "Comment tu vas à l'école chaque matin ?",
        "hint": "Explain your journey to school — transport, how long it takes, who you go with.",
        "model_answer": "Le matin, je prends le bus scolaire pour aller à l'école. Le trajet dure environ vingt minutes. Je retrouve mes amis à l'arrêt de bus et on discute pendant le voyage.",
        "follow_ups": ["À quelle heure tu pars de chez toi ?", "Est-ce que le trajet est long ?", "Tu préfères quel moyen de transport et pourquoi ?"],
        "key_vocab": [{"fr": "le trajet", "en": "the journey / commute"}, {"fr": "le bus scolaire", "en": "school bus"}, {"fr": "déposer quelqu'un", "en": "to drop someone off"}, {"fr": "en retard", "en": "late"}],
    },
    {
        "id": "sch_04", "topic_key": "school", "difficulty": 2, "is_past_paper": False,
        "text": "Est-ce que tu aimes ton école ? Pourquoi ou pourquoi pas ?",
        "hint": "Give a balanced opinion with both positives and negatives, plus reasons.",
        "model_answer": "Dans l'ensemble, j'aime bien mon école. Les professeurs sont compétents et bienveillants, et les installations sont modernes. Cependant, je pense que les journées sont trop longues et les devoirs excessifs.",
        "follow_ups": ["Qu'est-ce que tu changerais dans ton école si tu pouvais ?", "Les règles de ton école sont-elles strictes ?", "Quel est le meilleur souvenir que tu as de ton école ?"],
        "key_vocab": [{"fr": "dans l'ensemble", "en": "on the whole"}, {"fr": "bienveillant(e)", "en": "kind / caring"}, {"fr": "malgré tout", "en": "despite everything"}],
    },
    {
        "id": "sch_05", "topic_key": "school", "difficulty": 2, "is_past_paper": False,
        "text": "Qu'est-ce que tu as fait à l'école la semaine dernière ?",
        "hint": "Use passé composé to describe specific events — a lesson, a test, a school trip.",
        "model_answer": "La semaine dernière, j'ai eu un contrôle de maths assez difficile. J'avais bien révisé la veille, donc je pense que j'ai réussi. On a aussi fait une sortie scolaire au musée des sciences.",
        "follow_ups": ["Est-ce que tu as eu des examens récemment ?", "Tu as fait quelque chose de spécial avec ta classe ?", "Comment ça s'est passé ?"],
        "key_vocab": [{"fr": "un contrôle", "en": "a test / assessment"}, {"fr": "réviser", "en": "to revise / study"}, {"fr": "une sortie scolaire", "en": "a school trip"}, {"fr": "la veille", "en": "the day before"}],
    },
    {
        "id": "sch_06", "topic_key": "school", "difficulty": 1, "is_past_paper": False,
        "text": "Décris ta journée scolaire typique.",
        "hint": "Walk through your day from morning to end of school using time phrases.",
        "model_answer": "Ma journée scolaire commence à huit heures et demie. D'abord, on fait l'appel en classe de base, puis on va en cours. À midi, je mange à la cantine avec mes amis. Les cours se terminent à seize heures.",
        "follow_ups": ["Tu manges à la cantine ou tu apportes ton repas ?", "Combien de cours as-tu par jour ?", "À quelle heure finissent tes cours ?"],
        "key_vocab": [{"fr": "l'appel", "en": "the register / roll call"}, {"fr": "la cantine", "en": "the canteen"}, {"fr": "se terminer", "en": "to finish / end"}, {"fr": "d'abord... puis... ensuite...", "en": "first... then... next..."}],
    },
    # New school questions
    {
        "id": "sch_07", "topic_key": "school", "difficulty": 2, "is_past_paper": False,
        "text": "Décris ta routine du matin avant d'aller à l'école.",
        "hint": "Mention waking up, getting ready, breakfast, and transport — use reflexive verbs.",
        "follow_ups": ["À quelle heure est-ce que tu te lèves normalement ?", "Tu prends le petit-déjeuner avant de partir ?"],
        "key_vocab": [{"fr": "se lever", "en": "to get up"}, {"fr": "se préparer", "en": "to get ready"}, {"fr": "se dépêcher", "en": "to hurry"}, {"fr": "le petit-déjeuner", "en": "breakfast"}],
    },
    {
        "id": "sch_08", "topic_key": "school", "difficulty": 2, "is_past_paper": False,
        "text": "Parle d'un professeur qui t'a marqué(e).",
        "hint": "Describe who they are, what they teach, why they made an impression on you.",
        "follow_ups": ["Qu'est-ce que ce professeur t'a appris sur la vie ?", "Comment ce professeur rendait-il les cours intéressants ?"],
        "key_vocab": [{"fr": "marqué(e)", "en": "made an impression / memorable"}, {"fr": "inspirant(e)", "en": "inspiring"}, {"fr": "exigeant(e)", "en": "demanding / strict"}, {"fr": "encourager", "en": "to encourage"}],
    },
    {
        "id": "sch_09", "topic_key": "school", "difficulty": 2, "is_past_paper": False,
        "text": "Comment tu te prépares pour tes examens ?",
        "hint": "Describe your revision method — schedules, techniques, where you study.",
        "follow_ups": ["Est-ce que tu révises seul(e) ou avec des amis ?", "Quelles matières trouves-tu les plus difficiles à réviser ?"],
        "key_vocab": [{"fr": "réviser", "en": "to revise"}, {"fr": "un planning", "en": "a schedule"}, {"fr": "les fiches", "en": "revision cards / flashcards"}, {"fr": "se concentrer", "en": "to concentrate"}],
    },
    {
        "id": "sch_10", "topic_key": "school", "difficulty": 3, "is_past_paper": False,
        "text": "Que penses-tu du système scolaire dans ton pays ?",
        "hint": "Give a critical opinion — what works, what could be improved, compare with other countries.",
        "follow_ups": ["Penses-tu que les examens sont le meilleur moyen d'évaluer les élèves ?", "Est-ce que tu penses que l'école prépare bien les élèves à la vie adulte ?"],
        "key_vocab": [{"fr": "le système éducatif", "en": "the education system"}, {"fr": "évaluer", "en": "to assess / evaluate"}, {"fr": "la pression scolaire", "en": "academic pressure"}, {"fr": "les inégalités", "en": "inequalities"}],
    },
    {
        "id": "sch_11", "topic_key": "school", "difficulty": 2, "is_past_paper": False,
        "text": "As-tu participé à des activités extrascolaires ? Lesquelles ?",
        "hint": "Describe clubs, sports, or activities outside of regular class time.",
        "follow_ups": ["Pourquoi est-ce que tu t'es inscrit(e) à cette activité ?", "Qu'est-ce que ces activités t'ont apporté ?"],
        "key_vocab": [{"fr": "s'inscrire", "en": "to sign up / enrol"}, {"fr": "un club", "en": "a club"}, {"fr": "le bénévolat", "en": "volunteering"}, {"fr": "développer", "en": "to develop"}],
    },

    # ── LES LOISIRS ──────────────────────────────────────────────────────────
    {
        "id": "hob_01", "topic_key": "hobbies", "difficulty": 1, "is_past_paper": False,
        "text": "Qu'est-ce que tu fais pendant ton temps libre ?",
        "hint": "Describe 2-3 hobbies in detail — how often, who with, why you enjoy them.",
        "model_answer": "Pendant mon temps libre, j'aime surtout jouer de la guitare. Je joue depuis trois ans et je prends des cours le samedi. En plus, j'aime lire des romans — surtout les thrillers et la science-fiction.",
        "follow_ups": ["Depuis combien de temps tu fais cette activité ?", "Tu préfères les activités en plein air ou à l'intérieur ?", "Est-ce que tu voudrais essayer un nouveau passe-temps ?"],
        "key_vocab": [{"fr": "surtout", "en": "especially / above all"}, {"fr": "depuis", "en": "since / for (duration)"}, {"fr": "un passe-temps", "en": "a hobby / pastime"}],
    },
    {
        "id": "hob_02", "topic_key": "hobbies", "difficulty": 1, "is_past_paper": False,
        "text": "Tu fais du sport ? Quel sport tu préfères et pourquoi ?",
        "hint": "Talk about sports you play or watch. Include how often and where.",
        "model_answer": "Oui, je suis assez sportif(ve). Mon sport préféré, c'est le football — je joue dans l'équipe scolaire depuis deux ans. J'adore l'esprit d'équipe et la compétition.",
        "follow_ups": ["Tu fais partie d'une équipe ?", "Quel sport voudrais-tu apprendre à pratiquer ?", "Tu préfères regarder le sport à la télé ou y participer ?"],
        "key_vocab": [{"fr": "s'entraîner", "en": "to train / practise"}, {"fr": "l'esprit d'équipe", "en": "team spirit"}, {"fr": "la compétition", "en": "competition"}],
    },
    {
        "id": "hob_03", "topic_key": "hobbies", "difficulty": 1, "is_past_paper": False,
        "text": "Est-ce que tu joues d'un instrument de musique ou tu chantes ?",
        "hint": "Discuss any musical activity — playing, singing, concerts, favourite music genres.",
        "model_answer": "Je joue du piano depuis l'âge de sept ans. Au début, c'était difficile, mais maintenant j'en joue avec plaisir. Je m'entraîne environ trente minutes chaque jour.",
        "follow_ups": ["Quel est ton genre de musique préféré ?", "Tu écoutes de la musique française ?", "Tu es déjà allé(e) à un concert ?"],
        "key_vocab": [{"fr": "jouer de (+ instrument)", "en": "to play (an instrument)"}, {"fr": "au début", "en": "at first"}, {"fr": "inoubliable", "en": "unforgettable"}],
    },
    {
        "id": "hob_04", "topic_key": "hobbies", "difficulty": 2, "is_past_paper": False,
        "text": "Tu lis beaucoup ? Qu'est-ce que tu aimes lire ?",
        "hint": "Describe your reading habits, favourite genres, specific books or authors.",
        "model_answer": "Je lis assez souvent, surtout avant de dormir. J'aime beaucoup les romans d'aventure et les histoires de fantasy. Je pense que la lecture est très bénéfique parce qu'elle améliore le vocabulaire.",
        "follow_ups": ["Tu préfères les livres électroniques ou les livres papier ?", "Quel livre tu recommanderais à un ami ?", "Tu lis des livres en français ?"],
        "key_vocab": [{"fr": "un roman", "en": "a novel"}, {"fr": "captivant(e)", "en": "gripping / captivating"}, {"fr": "améliorer", "en": "to improve"}],
    },
    {
        "id": "hob_05", "topic_key": "hobbies", "difficulty": 2, "is_past_paper": False,
        "text": "Est-ce que tu regardes beaucoup la télévision ou des vidéos en ligne ?",
        "hint": "Discuss screen time habits — TV shows, YouTube, streaming, how much time per day.",
        "model_answer": "Je regarde des vidéos en ligne tous les jours, surtout sur YouTube. J'essaie de limiter mon temps d'écran à deux heures par jour pour ne pas négliger mes études.",
        "follow_ups": ["Quelle est ton émission préférée en ce moment ?", "Tu penses que les jeunes regardent trop la télé ?", "Est-ce que tu as regardé des films en français ?"],
        "key_vocab": [{"fr": "le temps d'écran", "en": "screen time"}, {"fr": "négliger", "en": "to neglect"}, {"fr": "un équilibre", "en": "a balance"}],
    },
    # New hobbies questions
    {
        "id": "hob_06", "topic_key": "hobbies", "difficulty": 2, "is_past_paper": False,
        "text": "Décris une activité que tu as essayée récemment pour la première fois.",
        "hint": "Use passé composé. Describe the activity, how it went, would you do it again?",
        "follow_ups": ["Comment tu as trouvé cette expérience ?", "Est-ce que tu recommanderais cette activité à tes amis ?"],
        "key_vocab": [{"fr": "pour la première fois", "en": "for the first time"}, {"fr": "tenter l'expérience", "en": "to try the experience"}, {"fr": "courageux/courageuse", "en": "brave"}],
    },
    {
        "id": "hob_07", "topic_key": "hobbies", "difficulty": 2, "is_past_paper": False,
        "text": "Parle d'un film ou d'une série que tu as récemment regardé(e).",
        "hint": "Describe the plot briefly, the genre, your opinion, and a recommendation.",
        "follow_ups": ["Quel acteur ou actrice préfères-tu ?", "Tu préfères les films d'action ou les comédies romantiques ?"],
        "key_vocab": [{"fr": "l'intrigue", "en": "the plot"}, {"fr": "un personnage", "en": "a character"}, {"fr": "je recommande", "en": "I recommend"}, {"fr": "émouvant(e)", "en": "moving / touching"}],
    },
    {
        "id": "hob_08", "topic_key": "hobbies", "difficulty": 3, "is_past_paper": False,
        "text": "Comment les loisirs des jeunes ont-ils changé avec internet ?",
        "hint": "Compare old and new leisure habits. Consider gaming, social media, streaming.",
        "follow_ups": ["Penses-tu que les jeunes passent trop de temps en ligne ?", "Quels sont les avantages et les inconvénients des loisirs numériques ?"],
        "key_vocab": [{"fr": "numérique", "en": "digital"}, {"fr": "virtuel(le)", "en": "virtual"}, {"fr": "s'isoler", "en": "to isolate oneself"}, {"fr": "les interactions sociales", "en": "social interactions"}],
    },
    {
        "id": "hob_09", "topic_key": "hobbies", "difficulty": 2, "is_past_paper": False,
        "text": "Qu'est-ce que tu fais pour te détendre après une longue journée ?",
        "hint": "Describe your relaxation routines — music, hobbies, social activities, rest.",
        "follow_ups": ["Est-ce que tu trouves facile de décompresser ?", "Qu'est-ce qui te stresse le plus dans ta vie ?"],
        "key_vocab": [{"fr": "se détendre", "en": "to relax"}, {"fr": "décompresser", "en": "to unwind"}, {"fr": "le bien-être", "en": "well-being"}, {"fr": "recharger les batteries", "en": "to recharge"}],
    },

    # ── LA FAMILLE ────────────────────────────────────────────────────────────
    {
        "id": "fam_01", "topic_key": "family", "difficulty": 1, "is_past_paper": False,
        "text": "Décris ta famille.",
        "hint": "Describe family members — their appearance, personality, job, and your relationship.",
        "model_answer": "Je vis avec mes parents et ma sœur cadette, qui a douze ans. Mon père est grand et brun — il travaille comme ingénieur. Ma mère a les cheveux châtains. Nous nous entendons bien en général.",
        "follow_ups": ["Tu t'entends bien avec tes frères et sœurs ?", "Qu'est-ce que ta famille fait ensemble le week-end ?", "Tu ressembles plus à ta mère ou à ton père ?"],
        "key_vocab": [{"fr": "cadet/cadette", "en": "younger (sibling)"}, {"fr": "s'entendre (bien)", "en": "to get along (well)"}, {"fr": "se disputer", "en": "to argue / quarrel"}],
    },
    {
        "id": "fam_02", "topic_key": "family", "difficulty": 2, "is_past_paper": False,
        "text": "Comment est ta relation avec tes parents ?",
        "hint": "Explain how you get along — what you do together, any conflicts, how they support you.",
        "model_answer": "Dans l'ensemble, j'ai une très bonne relation avec mes parents. Ils me soutiennent beaucoup, surtout avec mes études. Parfois, on n'est pas d'accord, mais on trouve toujours un compromis.",
        "follow_ups": ["Est-ce que tes parents sont stricts ?", "Qu'est-ce que tes parents font pour te soutenir à l'école ?"],
        "key_vocab": [{"fr": "soutenir", "en": "to support"}, {"fr": "un compromis", "en": "a compromise"}, {"fr": "respecter", "en": "to respect"}],
    },
    {
        "id": "fam_03", "topic_key": "family", "difficulty": 1, "is_past_paper": False,
        "text": "Décris ton meilleur ami ou ta meilleure amie.",
        "hint": "Describe their appearance, personality, how you met, what you do together.",
        "model_answer": "Mon meilleur ami s'appelle Liam. Il est grand, avec des cheveux blonds. On se connaît depuis l'école primaire. Il est vraiment drôle et loyal — je peux toujours compter sur lui.",
        "follow_ups": ["Depuis combien de temps tu connais cette personne ?", "Qu'est-ce que vous aimez faire ensemble ?"],
        "key_vocab": [{"fr": "compter sur quelqu'un", "en": "to rely on someone"}, {"fr": "loyal(e)", "en": "loyal"}, {"fr": "la confiance", "en": "trust"}],
    },
    {
        "id": "fam_04", "topic_key": "family", "difficulty": 1, "is_past_paper": False,
        "text": "Qu'est-ce que tu fais avec tes amis le week-end ?",
        "hint": "Talk about typical weekend activities with friends — where you go, what you do.",
        "model_answer": "Le week-end, j'aime retrouver mes amis en ville. On va souvent au cinéma ou dans un café. C'est toujours très sympa.",
        "follow_ups": ["Tu préfères rester à la maison ou sortir avec des amis ?", "Est-ce que tu utilises les réseaux sociaux pour rester en contact ?"],
        "key_vocab": [{"fr": "bavarder", "en": "to chat"}, {"fr": "se retrouver", "en": "to meet up"}, {"fr": "le bien-être", "en": "well-being"}],
    },
    # New family questions
    {
        "id": "fam_05", "topic_key": "family", "difficulty": 2, "is_past_paper": False,
        "text": "Décris une occasion spéciale que tu as célébrée avec ta famille.",
        "hint": "Use passé composé. Describe a birthday, holiday, or special event.",
        "follow_ups": ["Comment est-ce que ta famille célèbre les anniversaires ?", "Quelle est la fête préférée de ta famille ?"],
        "key_vocab": [{"fr": "fêter", "en": "to celebrate"}, {"fr": "une occasion", "en": "an occasion"}, {"fr": "réunir", "en": "to bring together"}, {"fr": "inoubliable", "en": "unforgettable"}],
    },
    {
        "id": "fam_06", "topic_key": "family", "difficulty": 3, "is_past_paper": False,
        "text": "Est-ce que tu penses que la famille est plus importante que les amis ?",
        "hint": "Give a nuanced opinion. Consider different types of support each provides.",
        "follow_ups": ["Comment est-ce que tes amis et ta famille se complètent ?", "Est-ce que les amis peuvent devenir une famille ?"],
        "key_vocab": [{"fr": "les liens familiaux", "en": "family bonds"}, {"fr": "se compléter", "en": "to complement each other"}, {"fr": "indispensable", "en": "indispensable"}],
    },
    {
        "id": "fam_07", "topic_key": "family", "difficulty": 2, "is_past_paper": False,
        "text": "Qu'est-ce que tes parents t'ont appris sur la vie ?",
        "hint": "Reflect on values, lessons, or skills your parents have given you.",
        "follow_ups": ["Quelle est la leçon la plus importante que tu as apprise de tes parents ?", "Est-ce que tu veux transmettre ces valeurs à tes enfants un jour ?"],
        "key_vocab": [{"fr": "transmettre", "en": "to pass on / transmit"}, {"fr": "les valeurs", "en": "values"}, {"fr": "l'autonomie", "en": "independence"}, {"fr": "le respect", "en": "respect"}],
    },
    {
        "id": "fam_08", "topic_key": "family", "difficulty": 2, "is_past_paper": False,
        "text": "Parle d'une tradition familiale importante pour toi.",
        "hint": "Describe a family tradition — what it is, how long you've done it, why it matters.",
        "follow_ups": ["Est-ce que cette tradition a changé au fil du temps ?", "Voudrais-tu garder cette tradition quand tu seras adulte ?"],
        "key_vocab": [{"fr": "une tradition", "en": "a tradition"}, {"fr": "se rassembler", "en": "to gather together"}, {"fr": "précieux/précieuse", "en": "precious"}, {"fr": "perpétuer", "en": "to perpetuate"}],
    },

    # ── LES VACANCES ──────────────────────────────────────────────────────────
    {
        "id": "hol_01", "topic_key": "holidays", "difficulty": 1, "is_past_paper": False,
        "text": "Où es-tu allé(e) pendant les dernières vacances ?",
        "hint": "Describe a recent holiday — where, who with, what you did, how you felt.",
        "model_answer": "L'été dernier, je suis allé(e) en Espagne avec ma famille. On a pris l'avion depuis Londres. On a séjourné dans un hôtel au bord de la mer, à Barcelone.",
        "follow_ups": ["Comment tu as voyagé — en avion, en voiture ou en train ?", "Qu'est-ce que tu as fait là-bas ?", "Est-ce que tu as mangé des spécialités locales ?"],
        "key_vocab": [{"fr": "séjourner", "en": "to stay (at a hotel etc.)"}, {"fr": "profiter de", "en": "to enjoy / make the most of"}, {"fr": "au bord de la mer", "en": "at the seaside"}],
    },
    {
        "id": "hol_02", "topic_key": "holidays", "difficulty": 2, "is_past_paper": False,
        "text": "Tu préfères les vacances à la mer ou à la montagne ? Pourquoi ?",
        "hint": "Compare both types of holiday, give strong reasons for your preference.",
        "model_answer": "Je préfère nettement les vacances à la mer. J'adore me baigner et me détendre sur la plage. Cependant, je reconnais que la montagne offre de beaux paysages.",
        "follow_ups": ["Qu'est-ce qu'on peut faire à la montagne en été ?", "Est-ce que les vacances à la campagne t'intéressent ?"],
        "key_vocab": [{"fr": "nettement", "en": "clearly / decidedly"}, {"fr": "se baigner", "en": "to swim / bathe"}, {"fr": "la randonnée", "en": "hiking"}, {"fr": "les paysages", "en": "landscapes"}],
    },
    {
        "id": "hol_03", "topic_key": "holidays", "difficulty": 3, "is_past_paper": False,
        "text": "Décris des vacances idéales.",
        "hint": "Paint a picture of your dream holiday — use conditional tense (j'irais, je ferais).",
        "model_answer": "Pour mes vacances idéales, j'irais au Japon avec mes meilleurs amis. On visiterait Tokyo, Kyoto et le Mont Fuji. Ce voyage serait parfait parce que le Japon mélange modernité et tradition.",
        "follow_ups": ["Avec qui est-ce que tu voyagerais dans l'idéal ?", "Tu préfères les hôtels de luxe ou le camping ?", "Quel pays voudrais-tu absolument visiter ?"],
        "key_vocab": [{"fr": "dans l'idéal", "en": "ideally"}, {"fr": "assister à", "en": "to attend"}, {"fr": "mélanger", "en": "to mix / combine"}],
    },
    {
        "id": "hol_04", "topic_key": "holidays", "difficulty": 2, "is_past_paper": False,
        "text": "Est-ce que tu voudrais voyager dans d'autres pays ? Lesquels ?",
        "hint": "Name specific countries, explain what appeals to you about each one.",
        "model_answer": "Oui, j'adorerais voyager beaucoup plus. J'aimerais visiter le Canada pour ses grands espaces sauvages. Je crois que voyager ouvre l'esprit et permet de découvrir d'autres cultures.",
        "follow_ups": ["Pourquoi est-ce que voyager est important, selon toi ?", "Les voyages t'ont-ils déjà changé ou appris quelque chose ?"],
        "key_vocab": [{"fr": "les grands espaces", "en": "wide open spaces"}, {"fr": "ouvrir l'esprit", "en": "to broaden the mind"}, {"fr": "rêver de", "en": "to dream of"}],
    },
    # New holidays questions
    {
        "id": "hol_05", "topic_key": "holidays", "difficulty": 2, "is_past_paper": False,
        "text": "Décris les vacances les plus mémorables de ta vie.",
        "hint": "Use past tenses. Describe what made them special — a moment, a place, or a person.",
        "follow_ups": ["Qu'est-ce qui a rendu ces vacances si spéciales ?", "Voudrais-tu y retourner ?"],
        "key_vocab": [{"fr": "mémorable", "en": "memorable"}, {"fr": "marquant(e)", "en": "remarkable / impactful"}, {"fr": "un souvenir", "en": "a memory"}, {"fr": "précieux/précieuse", "en": "precious"}],
    },
    {
        "id": "hol_06", "topic_key": "holidays", "difficulty": 2, "is_past_paper": False,
        "text": "Parle d'un pays francophone que tu aimerais visiter.",
        "hint": "Choose a French-speaking country. Explain what interests you — culture, food, sights.",
        "follow_ups": ["Pourquoi es-tu attiré(e) par ce pays ?", "Est-ce que tu essaierais de parler français là-bas ?"],
        "key_vocab": [{"fr": "francophone", "en": "French-speaking"}, {"fr": "la culture", "en": "culture"}, {"fr": "les monuments", "en": "monuments / landmarks"}, {"fr": "s'immerger", "en": "to immerse oneself"}],
    },
    {
        "id": "hol_07", "topic_key": "holidays", "difficulty": 3, "is_past_paper": False,
        "text": "Penses-tu que le tourisme de masse est mauvais pour l'environnement ?",
        "hint": "Give a balanced opinion. Consider carbon footprint, overcrowding, local culture.",
        "follow_ups": ["Qu'est-ce que le tourisme responsable ?", "As-tu déjà changé tes habitudes de voyage pour l'environnement ?"],
        "key_vocab": [{"fr": "le tourisme de masse", "en": "mass tourism"}, {"fr": "l'empreinte carbone", "en": "carbon footprint"}, {"fr": "la surpopulation", "en": "overcrowding"}, {"fr": "responsable", "en": "responsible"}],
    },
    {
        "id": "hol_08", "topic_key": "holidays", "difficulty": 2, "is_past_paper": False,
        "text": "Décris un problème que tu as eu pendant des vacances.",
        "hint": "Use passé composé. Describe the problem, how you solved it, what you learned.",
        "follow_ups": ["Comment as-tu résolu ce problème ?", "Est-ce que cela a gâché tes vacances ?"],
        "key_vocab": [{"fr": "un imprévu", "en": "an unexpected event"}, {"fr": "faire face à", "en": "to face / deal with"}, {"fr": "se débrouiller", "en": "to manage / cope"}, {"fr": "malgré tout", "en": "despite everything"}],
    },

    # ── LA MAISON ─────────────────────────────────────────────────────────────
    {
        "id": "hom_01", "topic_key": "home", "difficulty": 1, "is_past_paper": False,
        "text": "Décris ta maison ou ton appartement.",
        "hint": "Describe rooms, size, location, what you like or dislike about it.",
        "model_answer": "J'habite dans une maison semi-individuelle en banlieue. C'est une maison à deux étages avec quatre chambres, un salon, une cuisine moderne et un jardin.",
        "follow_ups": ["Ta chambre est comment ?", "Est-ce que tu partages une chambre avec quelqu'un ?"],
        "key_vocab": [{"fr": "semi-individuel(le)", "en": "semi-detached"}, {"fr": "en banlieue", "en": "in the suburbs"}, {"fr": "une armoire", "en": "a wardrobe"}, {"fr": "une affiche", "en": "a poster"}],
    },
    {
        "id": "hom_02", "topic_key": "home", "difficulty": 2, "is_past_paper": False,
        "text": "Comment est la ville ou le village où tu habites ?",
        "hint": "Describe your area — facilities, atmosphere, pros and cons for young people.",
        "model_answer": "J'habite dans une ville de taille moyenne. Il y a un centre commercial, plusieurs parcs, des cinémas et de bonnes liaisons de transport. Cependant, il manque des espaces culturels.",
        "follow_ups": ["Est-ce qu'il y a des problèmes dans ta ville ?", "Tu préférerais habiter à la campagne ou en ville ?"],
        "key_vocab": [{"fr": "de taille moyenne", "en": "medium-sized"}, {"fr": "les liaisons de transport", "en": "transport links"}, {"fr": "il manque", "en": "there is a lack of"}],
    },
    {
        "id": "hom_03", "topic_key": "home", "difficulty": 2, "is_past_paper": False,
        "text": "Qu'est-ce qu'il y a à faire pour les jeunes dans ta région ?",
        "hint": "Talk about leisure options — what's available, what's missing, what you'd add.",
        "model_answer": "Dans ma région, il y a une patinoire, plusieurs terrains de sport et des cafés. Cependant, je pense qu'il manque un espace artistique pour les jeunes.",
        "follow_ups": ["Est-ce que tu penses que ta ville fait assez pour les jeunes ?", "Les transports en commun sont-ils bien développés dans ta région ?"],
        "key_vocab": [{"fr": "une patinoire", "en": "an ice rink"}, {"fr": "la communauté", "en": "the community"}, {"fr": "un endroit", "en": "a place / spot"}],
    },
    # New home questions
    {
        "id": "hom_04", "topic_key": "home", "difficulty": 3, "is_past_paper": False,
        "text": "Si tu pouvais vivre n'importe où dans le monde, où irais-tu ?",
        "hint": "Use conditional tense. Name a city/country and explain your reasons.",
        "follow_ups": ["Qu'est-ce qui t'attire dans cet endroit ?", "Qu'est-ce qui te manquerait de ton pays si tu partais ?"],
        "key_vocab": [{"fr": "n'importe où", "en": "anywhere"}, {"fr": "s'installer", "en": "to settle / move to"}, {"fr": "le mode de vie", "en": "the lifestyle"}, {"fr": "s'adapter", "en": "to adapt"}],
    },
    {
        "id": "hom_05", "topic_key": "home", "difficulty": 2, "is_past_paper": False,
        "text": "Décris les avantages et les inconvénients de vivre en ville.",
        "hint": "Consider transport, culture, noise, pollution, social life, cost of living.",
        "follow_ups": ["Tu préférerais vivre en ville ou à la campagne quand tu seras adulte ?", "Qu'est-ce que tu ferais pour améliorer ta ville ?"],
        "key_vocab": [{"fr": "les avantages", "en": "advantages"}, {"fr": "les inconvénients", "en": "disadvantages"}, {"fr": "la pollution", "en": "pollution"}, {"fr": "le coût de la vie", "en": "cost of living"}],
    },
    {
        "id": "hom_06", "topic_key": "home", "difficulty": 2, "is_past_paper": False,
        "text": "Comment imagines-tu ta maison idéale ?",
        "hint": "Use conditional tense. Describe location, style, rooms, garden, neighbourhood.",
        "follow_ups": ["Préférerais-tu une maison à la campagne ou un appartement en ville ?", "Qu'est-ce qui est le plus important pour toi dans un logement ?"],
        "key_vocab": [{"fr": "idéal(e)", "en": "ideal"}, {"fr": "spacieux/spacieuse", "en": "spacious"}, {"fr": "le quartier", "en": "the neighbourhood"}, {"fr": "lumineux/lumineuse", "en": "bright / sunny"}],
    },
    {
        "id": "hom_07", "topic_key": "home", "difficulty": 2, "is_past_paper": False,
        "text": "Décris ce que tu fais chez toi pour aider ta famille.",
        "hint": "Talk about household chores and responsibilities. Do you think it's fair?",
        "follow_ups": ["Est-ce que les tâches ménagères sont partagées équitablement chez toi ?", "Penses-tu que les jeunes devraient avoir plus de responsabilités à la maison ?"],
        "key_vocab": [{"fr": "les tâches ménagères", "en": "household chores"}, {"fr": "faire la vaisselle", "en": "to do the washing-up"}, {"fr": "partager", "en": "to share"}, {"fr": "équitablement", "en": "fairly"}],
    },

    # ── L'AVENIR ──────────────────────────────────────────────────────────────
    {
        "id": "fut_01", "topic_key": "future", "difficulty": 2, "is_past_paper": False,
        "text": "Qu'est-ce que tu veux faire dans l'avenir ?",
        "hint": "Discuss future career or study plans. Use future tense and conditional.",
        "model_answer": "Dans l'avenir, j'aimerais devenir médecin. Je suis passionné(e) par les sciences et j'aime aider les autres. Pour réaliser ce rêve, je devrai obtenir de bonnes notes aux examens.",
        "follow_ups": ["Pourquoi tu as choisi cette carrière ?", "Qu'est-ce que tu dois faire pour réaliser ce rêve ?", "Est-ce que tu as un plan B si ça ne marche pas ?"],
        "key_vocab": [{"fr": "passionné(e) par", "en": "passionate about"}, {"fr": "réaliser", "en": "to achieve / fulfil"}, {"fr": "déterminé(e)", "en": "determined"}],
    },
    {
        "id": "fut_02", "topic_key": "future", "difficulty": 3, "is_past_paper": False,
        "text": "Tu voudrais aller à l'université ? Pourquoi ou pourquoi pas ?",
        "hint": "Give a clear opinion on university with pros/cons and alternatives.",
        "model_answer": "Oui, j'aimerais beaucoup aller à l'université. Je voudrais étudier l'informatique parce que c'est un domaine en pleine croissance. Je sais que les frais de scolarité sont élevés, mais c'est un investissement dans l'avenir.",
        "follow_ups": ["Qu'est-ce que tu voudrais étudier à l'université ?", "La dette étudiante te fait-elle peur ?"],
        "key_vocab": [{"fr": "les frais de scolarité", "en": "tuition fees"}, {"fr": "une dette", "en": "a debt"}, {"fr": "en pleine croissance", "en": "rapidly growing"}, {"fr": "un apprentissage", "en": "an apprenticeship"}],
    },
    {
        "id": "fut_03", "topic_key": "future", "difficulty": 2, "is_past_paper": False,
        "text": "Quel métier voudrais-tu faire plus tard ?",
        "hint": "Describe your dream job, why it appeals, what skills are needed.",
        "model_answer": "Plus tard, je voudrais travailler comme architecte. J'ai toujours été fasciné(e) par les bâtiments et j'adore dessiner. Ce métier me permettrait de combiner ma passion pour l'art et pour les sciences.",
        "follow_ups": ["Qu'est-ce qui t'a inspiré à vouloir ce métier ?", "Quelles qualités faut-il pour réussir dans ce domaine ?"],
        "key_vocab": [{"fr": "concevoir", "en": "to design / conceive"}, {"fr": "durable", "en": "sustainable"}, {"fr": "épanouissant(e)", "en": "fulfilling / rewarding"}, {"fr": "les compétences", "en": "skills"}],
    },
    # New future questions
    {
        "id": "fut_04", "topic_key": "future", "difficulty": 2, "is_past_paper": False,
        "text": "Quels sont tes projets pour après le lycée ?",
        "hint": "Describe your immediate post-school plans — gap year, university, work.",
        "follow_ups": ["Est-ce que tu as déjà des idées sur l'université ou la formation ?", "Est-ce que tes parents t'ont influencé dans tes choix ?"],
        "key_vocab": [{"fr": "après le lycée", "en": "after sixth form / high school"}, {"fr": "une année de césure", "en": "a gap year"}, {"fr": "une formation", "en": "training / course"}, {"fr": "envisager", "en": "to consider"}],
    },
    {
        "id": "fut_05", "topic_key": "future", "difficulty": 3, "is_past_paper": False,
        "text": "Comment est-ce que tu imagines ta vie dans dix ans ?",
        "hint": "Use future tense. Describe career, living situation, relationships, lifestyle.",
        "follow_ups": ["Penses-tu que tes rêves d'aujourd'hui seront réalisés dans dix ans ?", "Qu'est-ce que tu dois faire maintenant pour atteindre tes objectifs ?"],
        "key_vocab": [{"fr": "dans dix ans", "en": "in ten years"}, {"fr": "atteindre ses objectifs", "en": "to reach one's goals"}, {"fr": "s'épanouir", "en": "to flourish / thrive"}, {"fr": "indépendant(e)", "en": "independent"}],
    },
    {
        "id": "fut_06", "topic_key": "future", "difficulty": 3, "is_past_paper": False,
        "text": "Penses-tu que les jeunes d'aujourd'hui ont de bonnes perspectives d'avenir ?",
        "hint": "Consider climate change, job market, housing, technology. Be balanced.",
        "follow_ups": ["Quels défis les jeunes doivent-ils relever ?", "Es-tu optimiste ou pessimiste pour l'avenir de ta génération ?"],
        "key_vocab": [{"fr": "les perspectives", "en": "prospects / outlook"}, {"fr": "un défi", "en": "a challenge"}, {"fr": "optimiste/pessimiste", "en": "optimistic/pessimistic"}, {"fr": "l'avenir", "en": "the future"}],
    },
    {
        "id": "fut_07", "topic_key": "future", "difficulty": 3, "is_past_paper": False,
        "text": "Qu'est-ce que tu ferais si tu gagnais beaucoup d'argent ?",
        "hint": "Use conditional tense. Think about what matters to you — charity, travel, business.",
        "follow_ups": ["Est-ce que l'argent est la chose la plus importante dans la vie ?", "Qu'est-ce que tu ferais pour aider les autres avec cet argent ?"],
        "key_vocab": [{"fr": "dépenser", "en": "to spend"}, {"fr": "investir", "en": "to invest"}, {"fr": "faire un don", "en": "to make a donation"}, {"fr": "la générosité", "en": "generosity"}],
    },

    # ── LA NOURRITURE ─────────────────────────────────────────────────────────
    {
        "id": "foo_01", "topic_key": "food", "difficulty": 1, "is_past_paper": False,
        "text": "Qu'est-ce que tu manges normalement au déjeuner ?",
        "hint": "Describe your typical lunch — what, where, with whom. Include a past example.",
        "model_answer": "En général, je mange à la cantine avec mes amis. Je prends souvent un sandwich au fromage ou un repas chaud comme des pâtes avec des légumes.",
        "follow_ups": ["Tu apportes ton déjeuner de chez toi ou tu manges à la cantine ?", "C'est quoi ton repas préféré de la journée ?"],
        "key_vocab": [{"fr": "l'alimentation", "en": "diet / nutrition"}, {"fr": "sain(e)", "en": "healthy"}, {"fr": "un repas chaud", "en": "a hot meal"}],
    },
    {
        "id": "foo_02", "topic_key": "food", "difficulty": 2, "is_past_paper": False,
        "text": "Est-ce que tu fais attention à ta santé ?",
        "hint": "Discuss healthy habits — diet, exercise, sleep, screen time. Be honest!",
        "model_answer": "J'essaie de faire attention à ma santé. Je fais du sport trois fois par semaine. Concernant l'alimentation, je mange assez équilibré, mais j'avoue que j'aime trop le chocolat.",
        "follow_ups": ["Tu fais de l'exercice régulièrement ?", "La santé mentale est-elle aussi importante que la santé physique ?"],
        "key_vocab": [{"fr": "équilibré(e)", "en": "balanced"}, {"fr": "j'avoue que", "en": "I admit that"}, {"fr": "la santé mentale", "en": "mental health"}],
    },
    {
        "id": "foo_03", "topic_key": "food", "difficulty": 1, "is_past_paper": False,
        "text": "Quel est ton plat préféré ? Est-ce que tu sais le préparer ?",
        "hint": "Describe your favourite dish, its ingredients, whether you can cook it.",
        "model_answer": "Mon plat préféré, c'est la lasagne. J'adore le mélange de sauce bolognaise, de béchamel et de pâtes. Ma mère la prépare souvent le dimanche.",
        "follow_ups": ["Tu aimes cuisiner ? Qu'est-ce que tu sais faire ?", "Quelle cuisine étrangère tu préfères ?"],
        "key_vocab": [{"fr": "la sauce bolognaise", "en": "bolognese sauce"}, {"fr": "réussi(e)", "en": "successful / well done"}, {"fr": "une compétence", "en": "a skill"}],
    },
    # New food questions
    {
        "id": "foo_04", "topic_key": "food", "difficulty": 2, "is_past_paper": False,
        "text": "Décris un repas de fête typique dans ta famille.",
        "hint": "Describe a Christmas, birthday, or special occasion meal. Who cooks? What do you eat?",
        "follow_ups": ["Quelle est l'ambiance pendant ces repas de fête ?", "Est-ce que tu as une spécialité familiale ?"],
        "key_vocab": [{"fr": "un repas de fête", "en": "a festive meal"}, {"fr": "cuisiner", "en": "to cook"}, {"fr": "une recette", "en": "a recipe"}, {"fr": "se régaler", "en": "to enjoy a meal / feast"}],
    },
    {
        "id": "foo_05", "topic_key": "food", "difficulty": 3, "is_past_paper": False,
        "text": "Est-ce que le végétarisme est une bonne chose pour la santé et l'environnement ?",
        "hint": "Give a balanced opinion. Consider health, environment, culture, personal choice.",
        "follow_ups": ["Est-ce que tu as déjà essayé de manger végétarien ?", "Penses-tu que tout le monde devrait réduire sa consommation de viande ?"],
        "key_vocab": [{"fr": "végétarien(ne)", "en": "vegetarian"}, {"fr": "l'impact environnemental", "en": "environmental impact"}, {"fr": "la consommation de viande", "en": "meat consumption"}, {"fr": "durable", "en": "sustainable"}],
    },
    {
        "id": "foo_06", "topic_key": "food", "difficulty": 1, "is_past_paper": False,
        "text": "Décris une expérience culinaire inoubliable.",
        "hint": "Use past tenses. Describe a restaurant, a meal abroad, or someone's home cooking.",
        "follow_ups": ["Où était-ce et avec qui ?", "Est-ce que tu as essayé de refaire ce plat chez toi ?"],
        "key_vocab": [{"fr": "culinaire", "en": "culinary"}, {"fr": "savourer", "en": "to savour / enjoy"}, {"fr": "délicieux/délicieuse", "en": "delicious"}, {"fr": "inoubliable", "en": "unforgettable"}],
    },
    {
        "id": "foo_07", "topic_key": "food", "difficulty": 2, "is_past_paper": False,
        "text": "Que penses-tu de la restauration rapide ?",
        "hint": "Give a nuanced opinion — convenience vs. health, cost, environment.",
        "follow_ups": ["Est-ce que tu manges souvent au fast-food ?", "Penses-tu que les fast-foods devraient être interdits près des écoles ?"],
        "key_vocab": [{"fr": "la restauration rapide", "en": "fast food"}, {"fr": "pratique", "en": "convenient / practical"}, {"fr": "la malbouffe", "en": "junk food"}, {"fr": "interdit(e)", "en": "forbidden / banned"}],
    },

    # ── L'ENVIRONNEMENT ───────────────────────────────────────────────────────
    {
        "id": "env_01", "topic_key": "environment", "difficulty": 2, "is_past_paper": False,
        "text": "Qu'est-ce que tu fais pour protéger l'environnement ?",
        "hint": "Discuss specific eco-friendly actions you take at home, school, or in your community.",
        "model_answer": "Je fais plusieurs choses pour protéger l'environnement. Je recycle toujours le papier, le plastique et le verre. Je préfère aller à l'école à vélo pour réduire les émissions de carbone.",
        "follow_ups": ["Penses-tu que les gouvernements font assez pour l'environnement ?", "Est-ce que tu penses que les individus peuvent vraiment faire une différence ?"],
        "key_vocab": [{"fr": "recycler", "en": "to recycle"}, {"fr": "les émissions de carbone", "en": "carbon emissions"}, {"fr": "prendre des mesures", "en": "to take measures / steps"}],
    },
    {
        "id": "env_02", "topic_key": "environment", "difficulty": 2, "is_past_paper": False,
        "text": "Est-ce que la technologie joue un rôle important dans ta vie ?",
        "hint": "Discuss how technology affects your daily life — positives and negatives.",
        "model_answer": "Oui, la technologie joue un rôle énorme dans ma vie. J'utilise mon téléphone chaque jour pour communiquer, faire des recherches et écouter de la musique.",
        "follow_ups": ["Tu penses que les jeunes sont trop dépendants de la technologie ?", "L'intelligence artificielle — est-ce une bonne ou mauvaise chose ?"],
        "key_vocab": [{"fr": "dépendant(e) de", "en": "dependent on"}, {"fr": "néfaste", "en": "harmful / damaging"}, {"fr": "un équilibre sain", "en": "a healthy balance"}],
    },
    # New environment questions
    {
        "id": "env_03", "topic_key": "environment", "difficulty": 3, "is_past_paper": False,
        "text": "Que penses-tu du changement climatique et de ses conséquences ?",
        "hint": "Discuss causes, effects, and what should be done. Use evidence and examples.",
        "follow_ups": ["Le changement climatique t'inquiète-t-il personnellement ?", "Penses-tu que ta génération sera la plus touchée ?"],
        "key_vocab": [{"fr": "le changement climatique", "en": "climate change"}, {"fr": "les conséquences", "en": "consequences"}, {"fr": "les énergies renouvelables", "en": "renewable energies"}, {"fr": "s'engager", "en": "to commit / get involved"}],
    },
    {
        "id": "env_04", "topic_key": "environment", "difficulty": 2, "is_past_paper": False,
        "text": "Parle d'un problème environnemental qui t'inquiète particulièrement.",
        "hint": "Choose one issue (plastic, deforestation, pollution) and explain why it concerns you.",
        "follow_ups": ["Qu'est-ce que tu fais pour lutter contre ce problème ?", "Penses-tu que les jeunes peuvent changer les choses ?"],
        "key_vocab": [{"fr": "la déforestation", "en": "deforestation"}, {"fr": "la pollution plastique", "en": "plastic pollution"}, {"fr": "lutter contre", "en": "to fight against"}, {"fr": "inquiétant(e)", "en": "worrying"}],
    },
    {
        "id": "env_05", "topic_key": "environment", "difficulty": 3, "is_past_paper": False,
        "text": "L'énergie renouvelable — est-ce la solution à nos problèmes énergétiques ?",
        "hint": "Discuss solar, wind, nuclear energy. Consider cost, reliability, environmental impact.",
        "follow_ups": ["Quelle énergie renouvelable te semble la plus prometteuse ?", "Penses-tu que les combustibles fossiles peuvent être complètement abandonnés ?"],
        "key_vocab": [{"fr": "l'énergie renouvelable", "en": "renewable energy"}, {"fr": "les combustibles fossiles", "en": "fossil fuels"}, {"fr": "prometteuse", "en": "promising"}, {"fr": "fiable", "en": "reliable"}],
    },
    {
        "id": "env_06", "topic_key": "environment", "difficulty": 3, "is_past_paper": False,
        "text": "Qu'est-ce que tu penses de l'utilisation excessive du plastique ?",
        "hint": "Discuss the scale of the problem, its effects on oceans/wildlife, and solutions.",
        "follow_ups": ["Qu'est-ce que les entreprises peuvent faire pour réduire le plastique ?", "Utilises-tu des alternatives au plastique dans ta vie quotidienne ?"],
        "key_vocab": [{"fr": "le plastique à usage unique", "en": "single-use plastic"}, {"fr": "les océans", "en": "oceans"}, {"fr": "biodégradable", "en": "biodegradable"}, {"fr": "interdire", "en": "to ban"}],
    },
    {
        "id": "env_07", "topic_key": "environment", "difficulty": 3, "is_past_paper": False,
        "text": "Comment la technologie peut-elle aider à résoudre les problèmes environnementaux ?",
        "hint": "Think about electric cars, solar panels, AI for energy management, recycling tech.",
        "follow_ups": ["Est-ce que tu fais confiance à la technologie pour sauver la planète ?", "Y a-t-il des risques à dépendre trop de la technologie pour résoudre les problèmes ?"],
        "key_vocab": [{"fr": "l'intelligence artificielle", "en": "artificial intelligence"}, {"fr": "les panneaux solaires", "en": "solar panels"}, {"fr": "les voitures électriques", "en": "electric cars"}, {"fr": "l'innovation", "en": "innovation"}],
    },
    {
        "id": "env_08", "topic_key": "environment", "difficulty": 3, "is_past_paper": False,
        "text": "Décris ce que serait le monde dans 50 ans si on ne prend pas soin de la planète.",
        "hint": "Use conditional/future. Describe rising temperatures, extreme weather, species loss.",
        "follow_ups": ["Est-ce que tu es optimiste ou pessimiste pour l'avenir de notre planète ?", "Qu'est-ce qui doit changer immédiatement ?"],
        "key_vocab": [{"fr": "dans 50 ans", "en": "in 50 years"}, {"fr": "les espèces menacées", "en": "endangered species"}, {"fr": "les catastrophes naturelles", "en": "natural disasters"}, {"fr": "la survie", "en": "survival"}],
    },
]

# ── DAILY CHALLENGES ──────────────────────────────────────────────────────────
DAILY_CHALLENGES = [
    {
        "text": "Décris la dernière fois que tu as été vraiment heureux/heureuse.",
        "hint": "Use passé composé. Describe the event, who was there, what happened, how you felt.",
        "topic": "Expression personnelle",
        "difficulty": 2,
        "active_date": None,
    },
    {
        "text": "Si tu pouvais changer une chose dans le monde, qu'est-ce que ce serait ?",
        "hint": "Use the conditional. Be specific and give reasons for your choice.",
        "topic": "Opinion",
        "difficulty": 3,
        "active_date": None,
    },
    {
        "text": "Décris une personne qui t'inspire vraiment.",
        "hint": "Describe who they are, what they do, why they inspire you. Could be real or fictional.",
        "topic": "Inspiration",
        "difficulty": 2,
        "active_date": None,
    },
    {
        "text": "Qu'est-ce que tu as appris de tes erreurs ?",
        "hint": "Talk about a mistake you made and what lesson you drew from it.",
        "topic": "Réflexion personnelle",
        "difficulty": 3,
        "active_date": None,
    },
    {
        "text": "Décris un moment où tu as dû faire preuve de courage.",
        "hint": "Describe a situation that required bravery — standing up for someone, trying something new.",
        "topic": "Expérience personnelle",
        "difficulty": 2,
        "active_date": None,
    },
    {
        "text": "Quelles sont les qualités les plus importantes d'un bon ami ?",
        "hint": "List 3+ qualities with examples. You can compare a good friend vs. a bad friend.",
        "topic": "Les amis",
        "difficulty": 1,
        "active_date": None,
    },
    {
        "text": "Comment est-ce que les réseaux sociaux ont changé la façon dont les jeunes communiquent ?",
        "hint": "Discuss both positive and negative impacts. Include your own experience.",
        "topic": "Technologie & Société",
        "difficulty": 3,
        "active_date": None,
    },
    {
        "text": "Parle d'un moment où tu as aidé quelqu'un.",
        "hint": "Describe what happened, what you did, and how it made you feel.",
        "topic": "Entraide",
        "difficulty": 2,
        "active_date": None,
    },
    {
        "text": "Est-ce que les jeunes ont trop de pression aujourd'hui ? Pourquoi ?",
        "hint": "Consider academic pressure, social media, family expectations, future worries.",
        "topic": "Société",
        "difficulty": 3,
        "active_date": None,
    },
    {
        "text": "Décris ton rêve le plus fou.",
        "hint": "Use conditional tense. Describe an ambitious dream — career, travel, achievement.",
        "topic": "Rêves et ambitions",
        "difficulty": 2,
        "active_date": None,
    },
]

# ── EXAM SETS ─────────────────────────────────────────────────────────────────
EXAM_SETS = [
    {
        "id": "exam_set_1",
        "label": "Exam Set A — General",
        "question_ids": ["sch_01", "hob_01", "fam_01", "hol_01", "fut_01"],
        "tier": "free",
        "is_active": True,
    },
    {
        "id": "exam_set_2",
        "label": "Exam Set B — Personal & Social",
        "question_ids": ["fam_03", "hob_02", "hom_01", "foo_01", "env_01"],
        "tier": "free",
        "is_active": True,
    },
    {
        "id": "exam_set_3",
        "label": "Exam Set C — Extended Tier",
        "question_ids": ["sch_04", "hol_03", "fut_02", "env_02", "hob_04"],
        "tier": "free",
        "is_active": True,
    },
    {
        "id": "exam_set_4",
        "label": "Exam Set D — Everyday Life",
        "question_ids": ["foo_01", "hom_01", "hob_05", "fam_04", "sch_06"],
        "tier": "free",
        "is_active": True,
    },
    {
        "id": "exam_set_5",
        "label": "Exam Set E — Future & Society",
        "question_ids": ["fut_01", "env_03", "sch_10", "hob_08", "fut_06"],
        "tier": "free",
        "is_active": True,
    },
]


# ── Seed functions ────────────────────────────────────────────────────────────
def seed_questions():
    print(f"Seeding {len(QUESTIONS)} questions...")
    # Upsert in batches of 20
    batch_size = 20
    for i in range(0, len(QUESTIONS), batch_size):
        batch = QUESTIONS[i:i + batch_size]
        # Ensure required defaults
        for q in batch:
            q.setdefault("follow_ups", [])
            q.setdefault("key_vocab", [])
            q.setdefault("year", None)
            q.setdefault("paper_type", None)
            q.setdefault("paper_code", None)
            q.setdefault("question_number", None)
            q.setdefault("model_answer", None)
            q.setdefault("hint", None)
            q["is_active"] = True
        db.table("questions").upsert(batch, on_conflict="id").execute()
        print(f"  ✓ Questions {i + 1}–{min(i + batch_size, len(QUESTIONS))} done")


def seed_daily_challenges():
    print(f"Seeding {len(DAILY_CHALLENGES)} daily challenges...")
    for ch in DAILY_CHALLENGES:
        ch.setdefault("is_active", True)
    db.table("daily_challenges").upsert(DAILY_CHALLENGES).execute()
    print("  ✓ Daily challenges done")


def seed_exam_sets():
    print(f"Seeding {len(EXAM_SETS)} exam sets...")
    db.table("exam_sets").upsert(EXAM_SETS, on_conflict="id").execute()
    print("  ✓ Exam sets done")


if __name__ == "__main__":
    print("=== French Coach — Supabase Seed ===")
    seed_questions()
    seed_daily_challenges()
    seed_exam_sets()
    print(f"\n✅ Done! {len(QUESTIONS)} questions, {len(DAILY_CHALLENGES)} challenges, {len(EXAM_SETS)} exam sets seeded.")
