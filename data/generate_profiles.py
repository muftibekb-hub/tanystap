"""Генератор синтетических анкет участников IT-мероприятия.

Все имена и компании вымышлены. Запуск:
    python data/generate_profiles.py --n 200
Создаёт data/profiles.json и data/profiles.csv
"""
import argparse
import csv
import json
import random
from pathlib import Path

GOALS = ["job", "hire", "find_mentor", "mentor", "cofounder", "exchange"]
# Баланс целей: соискателям нужны нанимающие, джунам — менторы
GOAL_WEIGHTS = {"job": 22, "hire": 16, "find_mentor": 14, "mentor": 14, "cofounder": 12, "exchange": 22}

FIRST_KZ = ["Айгерим", "Ерлан", "Данияр", "Асель", "Нурлан", "Жанна", "Арман", "Динара", "Бауыржан", "Мадина",
            "Ерасыл", "Аружан", "Темирлан", "Камила", "Санжар", "Айдана", "Алихан", "Томирис", "Ержан", "Гульназ"]
LAST_KZ = ["Сагынбаев", "Жумабеков", "Оспанов", "Касымов", "Нурпеисов", "Абенов", "Тулегенов", "Ахметов",
           "Сейткали", "Бекмуратов", "Искаков", "Мусин", "Есенов", "Карибаев"]
FIRST_RU = ["Алексей", "Мария", "Дмитрий", "Екатерина", "Илья", "Анна", "Сергей", "Ольга", "Никита", "Виктория"]
LAST_RU = ["Ковалев", "Смирнов", "Белов", "Орлов", "Лебедев", "Морозов", "Волков", "Зайцев"]
FIRST_EN = ["Alex", "Sam", "Emily", "Daniel", "Sofia", "Lucas", "Mia", "Ryan"]
LAST_EN = ["Carter", "Hughes", "Bennett", "Foster", "Reed", "Walsh"]

COMPANIES = ["Qazyna Pay", "SteppeSoft", "Altyn Data", "Nomad Cloud", "BaiTech", "Tulpar Labs", "Kokpar Games",
             "Shanyraq Systems", "Aqsu Analytics", "Samruk Digital Lab", "Zhebe Logistics", "Orda Fintech",
             "Saryarqa AI", "Medeu Health"]

ROLES = {
    "backend": ("Backend-разработчик", ["Python", "Go", "Java", "Node.js", "PostgreSQL", "Kafka", "Redis", "Docker", "Django", "FastAPI", "Spring", "C#", ".NET"]),
    "frontend": ("Frontend-разработчик", ["JavaScript", "TypeScript", "React", "Vue", "Next.js", "CSS", "Figma"]),
    "mobile": ("Mobile-разработчик", ["Kotlin", "Swift", "Flutter", "Dart", "React Native", "Android", "iOS"]),
    "data": ("Data Scientist / ML", ["Python", "PyTorch", "TensorFlow", "SQL", "pandas", "scikit-learn", "LLM", "NLP", "Computer Vision", "Spark"]),
    "analytics": ("Аналитик данных", ["SQL", "Python", "Power BI", "Tableau", "Excel", "A/B тесты", "ClickHouse"]),
    "devops": ("DevOps-инженер", ["Docker", "Kubernetes", "Terraform", "AWS", "GCP", "CI/CD", "Linux", "Ansible", "Grafana"]),
    "qa": ("QA-инженер", ["Selenium", "Python", "Java", "Postman", "Playwright", "Автотесты", "JMeter"]),
    "design": ("UI/UX-дизайнер", ["Figma", "UX Research", "Prototyping", "Design Systems", "Adobe"]),
    "product": ("Product Manager", ["Jira", "Аналитика", "Customer Development", "Roadmap", "SQL", "Unit-экономика"]),
    "security": ("Специалист по ИБ", ["Pentest", "SOC", "Linux", "Python", "OWASP", "SIEM"]),
}
HIRER_ROLES = ["Team Lead", "CTO", "IT-рекрутер", "Head of Engineering", "Engineering Manager"]
BUSINESS_ROLES = ["Предприниматель", "Бизнес-девелопер", "Основатель стартапа"]

INTERESTS = ["финтех", "GovTech", "EdTech", "e-commerce", "LLM и генеративный AI", "компьютерное зрение",
             "высоконагруженные системы", "микросервисы", "open source", "геймдев", "кибербезопасность",
             "стартапы", "MedTech", "логистика", "AgroTech", "блокчейн", "облачная инфраструктура",
             "продуктовая аналитика", "мобильные приложения", "UX", "карьера в IT", "менторство",
             "автоматизация тестирования", "Data Engineering", "умный город"]

# Шаблоны «о себе»: ключ — (цель, язык)
ABOUT = {
    ("job", "ru"): "{role} с опытом {exp} {years}. Работаю с {stack}. Интересуюсь темами: {interests}. Ищу команду, где смогу расти, рассматриваю офис в Алматы/Астане или удалёнку.",
    ("job", "kk"): "{exp} жыл тәжірибесі бар {role}. {stack} қолданамын. Қызығушылықтарым: {interests}. Дамуға мүмкіндік беретін командада жұмыс іздеп жүрмін.",
    ("job", "en"): "{role} with {exp} years of experience. I work with {stack}. Interested in {interests}. Looking for a team where I can grow.",
    ("hire", "ru"): "{role} в {company}. Нанимаем {target} (стек: {stack}). Делаем продукты в сфере: {interests}. Готовы рассмотреть кандидатов разного уровня.",
    ("hire", "kk"): "{company} компаниясында {role}. {target} іздеп жүрміз ({stack}). Бағытымыз: {interests}.",
    ("hire", "en"): "{role} at {company}. We are hiring {target} ({stack}). Our focus: {interests}.",
    ("find_mentor", "ru"): "Начинающий {role}, опыт {exp} {years}. Учу {stack}. Ищу ментора, который поможет с карьерным ростом и код-ревью. Интересно: {interests}.",
    ("find_mentor", "kk"): "Бастаушы {role}, тәжірибем {exp} жыл. {stack} үйреніп жүрмін. Мансаптық өсуге көмектесетін ментор іздеймін. Қызығушылықтарым: {interests}.",
    ("find_mentor", "en"): "Junior {role} with {exp} years of experience, learning {stack}. Looking for a mentor to help with career growth. Interested in {interests}.",
    ("mentor", "ru"): "{role} с опытом {exp} {years}, сейчас в {company}. Эксперт в {stack}. Готов менторить джунов и мидлов: код-ревью, карьера, собеседования. Темы: {interests}.",
    ("mentor", "kk"): "{exp} жыл тәжірибесі бар {role}, {company} компаниясында. {stack} бойынша сарапшымын. Жас мамандарға менторлық жасауға дайынмын. Тақырыптар: {interests}.",
    ("mentor", "en"): "{role} with {exp} years of experience at {company}. Expert in {stack}. Happy to mentor juniors: code review, career, interviews. Topics: {interests}.",
    ("cofounder", "ru"): "{role}, опыт {exp} {years}. Есть идея стартапа в сфере {interests}. Ищу кофаундера: {target}. Мой стек/навыки: {stack}.",
    ("cofounder", "kk"): "{role}, тәжірибем {exp} жыл. {interests} саласында стартап идеям бар. Кофаундер іздеймін: {target}. Дағдыларым: {stack}.",
    ("cofounder", "en"): "{role}, {exp} years of experience. I have a startup idea in {interests}. Looking for a co-founder: {target}. My skills: {stack}.",
    ("exchange", "ru"): "{role}, {exp} {years} в индустрии. Работаю с {stack}. Хочу обменяться опытом с коллегами по темам: {interests}.",
    ("exchange", "kk"): "{role}, салада {exp} жыл. {stack} қолданамын. Әріптестермен тәжірибе алмасқым келеді: {interests}.",
    ("exchange", "en"): "{role}, {exp} years in the industry. I work with {stack}. Keen to exchange experience on {interests}.",
}

TECH_FAMILIES = ["backend", "frontend", "mobile", "data", "devops"]


def years_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "год"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "года"
    return "лет"


def make_name(lang: str, used: set) -> str:
    while True:
        if lang == "en":
            name = f"{random.choice(FIRST_EN)} {random.choice(LAST_EN)}"
        elif random.random() < 0.8:
            first = random.choice(FIRST_KZ)
            last = random.choice(LAST_KZ)
            if first.endswith(("а", "я")) or first in {"Асель", "Аружан", "Томирис", "Гульназ", "Айгерим", "Жанна"}:
                last = last + "а" if not last.endswith("и") else last
            name = f"{first} {last}"
        else:
            first = random.choice(FIRST_RU)
            last = random.choice(LAST_RU)
            if first in {"Мария", "Екатерина", "Анна", "Ольга", "Виктория"}:
                last += "а"
            name = f"{first} {last}"
        if name not in used:
            used.add(name)
            return name


def make_profile(i: int, goal: str, used: set) -> dict:
    lang = random.choices(["ru", "kk", "en"], weights=[60, 25, 15])[0]
    family = random.choice(list(ROLES))
    role_title, stack_pool = ROLES[family]
    stack = random.sample(stack_pool, k=min(len(stack_pool), random.randint(3, 5)))
    interests = random.sample(INTERESTS, k=random.randint(2, 4))
    company = random.choice(COMPANIES)
    target = ""

    if goal == "find_mentor":
        exp = random.randint(0, 2)
    elif goal == "mentor":
        exp = random.randint(5, 15)
    elif goal == "hire":
        exp = random.randint(4, 14)
        target_title = ROLES[family][0]
        role_title = random.choice(HIRER_ROLES)
        if role_title in {"Team Lead"}:
            role_title = f"Team Lead ({target_title})"
        target = f"{target_title}" if lang == "ru" else target_title
    elif goal == "cofounder":
        exp = random.randint(2, 10)
        if random.random() < 0.35:
            role_title = random.choice(BUSINESS_ROLES)
            family = "business"
            stack = random.sample(["Продажи", "Маркетинг", "Фандрайзинг", "Customer Development", "Unit-экономика", "Партнёрства"], k=3)
            target = {"ru": "технический кофаундер (CTO)", "kk": "техникалық кофаундер (CTO)", "en": "technical co-founder (CTO)"}[lang]
        else:
            target = {"ru": "бизнес-кофаундер (продажи, продукт)", "kk": "бизнес-кофаундер (сату, өнім)", "en": "business co-founder (sales, product)"}[lang]
    else:
        exp = random.randint(1, 10)

    about = ABOUT[(goal, lang)].format(
        role=role_title, exp=exp, years=years_word(exp), stack=", ".join(stack),
        interests=", ".join(interests), company=company, target=target,
    )
    return {
        "id": f"p{i:03d}",
        "name": make_name(lang, used),
        "role": role_title,
        "role_family": family,
        "stack": stack,
        "experience": exp,
        "interests": interests,
        "about": about,
        "goal": goal,
        "lang": lang,
        "company": company if goal in {"hire", "mentor"} else "",
        "hiring_for": target if goal == "hire" else "",
        "slots": sorted(random.sample(["11:00", "11:30", "12:00", "13:30", "14:00", "14:30", "15:00", "16:00"], k=3)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    random.seed(args.seed)

    goals = random.choices(list(GOAL_WEIGHTS), weights=list(GOAL_WEIGHTS.values()), k=args.n)
    used: set = set()
    profiles = [make_profile(i + 1, g, used) for i, g in enumerate(goals)]

    out = Path(__file__).parent
    (out / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out / "profiles.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(profiles[0]))
        w.writeheader()
        for p in profiles:
            w.writerow({**p, "stack": "; ".join(p["stack"]), "interests": "; ".join(p["interests"]), "slots": "; ".join(p["slots"])})
    print(f"Сгенерировано {len(profiles)} анкет -> {out/'profiles.json'}")


if __name__ == "__main__":
    main()
