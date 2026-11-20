"""Объяснения «почему вам стоит познакомиться» и первая фраза.

По умолчанию — шаблоны на основе фактов (мгновенно, без ключей).
Если задан LLM_API_KEY, текст улучшает LLM (любой OpenAI-совместимый API) с таймаутом;
при ошибке или таймауте остаются шаблоны, поэтому ответ всегда < 3 секунд.
"""
import json
import os

import requests

GOAL_PAIR = {
    "ru": {
        ("job", "hire"): "{name} ищет в {company}: {target}, а вы как раз ищете работу",
        ("hire", "job"): "{name} ищет работу, а вы нанимаете",
        ("find_mentor", "mentor"): "{name} готов(а) менторить, опыт {exp} лет",
        ("mentor", "find_mentor"): "{name} ищет ментора, а вы готовы им стать",
        ("cofounder", "cofounder"): "{name} тоже ищет кофаундера",
        ("exchange", "exchange"): "{name} тоже хочет обменяться опытом",
        "default": "{name} — {role}, может быть полезен(на) по вашей цели",
    },
    "kk": {
        ("job", "hire"): "{name} {company} компаниясына маман іздейді ({target}), ал сіз жұмыс іздеп жүрсіз",
        ("hire", "job"): "{name} жұмыс іздеп жүр, ал сіз маман іздейсіз",
        ("find_mentor", "mentor"): "{name} менторлық жасауға дайын, тәжірибесі {exp} жыл",
        ("mentor", "find_mentor"): "{name} ментор іздейді, ал сіз ментор болуға дайынсыз",
        ("cofounder", "cofounder"): "{name} де кофаундер іздеп жүр",
        ("exchange", "exchange"): "{name} де тәжірибе алмасқысы келеді",
        "default": "{name} — {role}, сіздің мақсатыңызға пайдалы болуы мүмкін",
    },
    "en": {
        ("job", "hire"): "{name} is hiring at {company} ({target}), and you are looking for a job",
        ("hire", "job"): "{name} is looking for a job, and you are hiring",
        ("find_mentor", "mentor"): "{name} is open to mentoring, with {exp} years of experience",
        ("mentor", "find_mentor"): "{name} is looking for a mentor, and you are ready to be one",
        ("cofounder", "cofounder"): "{name} is also looking for a co-founder",
        ("exchange", "exchange"): "{name} also wants to exchange experience",
        "default": "{name} is a {role} who can help with your goal",
    },
}
COMMON = {
    "ru": {"stack": "общий стек: {x}", "interests": "общие интересы: {x}", "cofounder": "роли дополняют друг друга"},
    "kk": {"stack": "ортақ стек: {x}", "interests": "ортақ қызығушылықтар: {x}", "cofounder": "рөлдеріңіз бір-бірін толықтырады"},
    "en": {"stack": "shared stack: {x}", "interests": "shared interests: {x}", "cofounder": "your roles complement each other"},
}
ICEBREAKER = {
    "ru": {
        "hire": "Привет! Вижу, вы нанимаете в {company}. Расскажете, какая сейчас задача у команды?",
        "job": "Привет! Я видел(а), что ты ищешь работу. Давай расскажу, чем мы занимаемся?",
        "mentor": "Здравствуйте! Хочу расти в {topic}. Можно задать пару вопросов про ваш путь?",
        "find_mentor": "Привет! Я могу помочь с {topic}. Расскажи, где сейчас сложнее всего?",
        "cofounder": "Привет! Тоже думаю о стартапе в теме «{topic}». Обсудим идеи за кофе?",
        "exchange": "Привет! Вижу, ты тоже работаешь с {topic}. Как у вас это устроено?",
    },
    "kk": {
        "hire": "Сәлеметсіз бе! {company} компаниясына маман іздеп жатқаныңызды көрдім. Команда туралы айтып бересіз бе?",
        "job": "Сәлем! Жұмыс іздеп жүргеніңді көрдім. Біздің команда туралы айтып берейін бе?",
        "mentor": "Сәлеметсіз бе! {topic} бағытында өскім келеді. Тәжірибеңіз туралы сұрасам бола ма?",
        "find_mentor": "Сәлем! {topic} бойынша көмектесе аламын. Қазір не қиын?",
        "cofounder": "Сәлем! Мен де «{topic}» тақырыбында стартап туралы ойлап жүрмін. Кофе ішіп талқылайық па?",
        "exchange": "Сәлем! Сен де {topic} қолданады екенсің. Сендерде бұл қалай ұйымдастырылған?",
    },
    "en": {
        "hire": "Hi! I saw you're hiring at {company}. What is the team working on right now?",
        "job": "Hi! I noticed you're looking for a job. Want to hear what our team does?",
        "mentor": "Hi! I want to grow in {topic}. Could I ask a couple of questions about your path?",
        "find_mentor": "Hi! I could help with {topic}. What's the hardest part for you right now?",
        "cofounder": "Hi! I'm also thinking about a startup in {topic}. Coffee and swap ideas?",
        "exchange": "Hi! I see you also work with {topic}. How do you approach it?",
    },
}


def template_explain(q: dict, r: dict, lang: str) -> tuple[str, str]:
    lang = lang if lang in GOAL_PAIR else "ru"
    b = r["profile"]
    name = b["name"].split()[0]
    pair_t = GOAL_PAIR[lang].get((q["goal"], b["goal"]), GOAL_PAIR[lang]["default"])
    first = pair_t.format(name=name, company=b.get("company") or "—", exp=b["experience"], role=b["role"],
                          target=b.get("hiring_for") or b["role"])
    extras = []
    if q["goal"] == "cofounder" and b["goal"] == "cofounder" and r["breakdown"].get("role", 0) >= 1:
        extras.append(COMMON[lang]["cofounder"])
    if r["common_stack"]:
        extras.append(COMMON[lang]["stack"].format(x=", ".join(r["common_stack"][:3])))
    if r["common_interests"]:
        extras.append(COMMON[lang]["interests"].format(x=", ".join(r["common_interests"][:2])))
    tail = "; ".join(extras)
    text = first + (". " + tail[:1].upper() + tail[1:] if extras else "") + "."

    topic = (r["common_stack"] or r["common_interests"] or b["interests"] or b["stack"] or ["IT"])[0]
    ice_key = b["goal"]
    if (ice_key == "job" and q["goal"] != "hire") or (ice_key == "find_mentor" and q["goal"] != "mentor"):
        ice_key = "exchange"  # фраза «давай расскажу о нашей команде» уместна только от нанимающего
    ice = ICEBREAKER[lang].get(ice_key, ICEBREAKER[lang]["exchange"]).format(company=b.get("company") or "", topic=topic)
    return text, ice


LANG_NAME = {"ru": "русском", "kk": "казахском", "en": "английском"}


def llm_explain(q: dict, results: list[dict], lang: str, timeout: float = 2.2) -> list[dict] | None:
    key = os.getenv("LLM_API_KEY")
    if not key:
        return None
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    me = {k: q.get(k) for k in ["role", "stack", "experience", "interests", "about", "goal"]}
    cands = [{"id": r["profile"]["id"], **{k: r["profile"][k] for k in ["name", "role", "stack", "experience", "interests", "about", "goal", "company"]},
              "common_stack": r["common_stack"], "common_interests": r["common_interests"]} for r in results]
    prompt = (
        f"Ты помогаешь участнику IT-мероприятия знакомиться. Анкета участника: {json.dumps(me, ensure_ascii=False)}\n"
        f"Кандидаты: {json.dumps(cands, ensure_ascii=False)}\n"
        f"Для каждого кандидата на {LANG_NAME.get(lang, 'русском')} языке напиши: 'why' — 1–2 предложения, почему стоит познакомиться, "
        "опираясь только на конкретные факты из анкет (цели, стек, опыт, интересы); 'icebreaker' — одну естественную первую фразу. "
        'Ответь JSON: {"items":[{"id":"...","why":"...","icebreaker":"..."}]}'
    )
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "response_format": {"type": "json_object"}, "temperature": 0.4},
            timeout=timeout,
        )
        resp.raise_for_status()
        items = json.loads(resp.json()["choices"][0]["message"]["content"])["items"]
        return items
    except Exception as e:  # таймаут, лимиты, кривой JSON — молча падаем на шаблоны
        print("LLM fallback:", repr(e)[:200])
        return None


def explain_all(q: dict, results: list[dict], lang: str) -> list[dict]:
    for r in results:
        r["why"], r["icebreaker"] = template_explain(q, r, lang)
        r["source"] = "template"
    items = llm_explain(q, results, lang)
    if items:
        by_id = {it.get("id"): it for it in items}
        for r in results:
            it = by_id.get(r["profile"]["id"])
            if it and it.get("why"):
                r["why"], r["icebreaker"], r["source"] = it["why"], it.get("icebreaker") or r["icebreaker"], "llm"
    return results
