import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import hmac
import secrets

from flask import Flask, jsonify, redirect, request, send_from_directory, session as web_session

from explain import explain_all
from recommender import GOAL_MATRIX, Recommender

ROOT = Path(__file__).resolve().parent

# Читаем ключи из .env (в корне проекта), если он есть
for env_file in (ROOT.parent / ".env", ROOT / ".env"):
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

DATA = Path(os.getenv("PROFILES_PATH", ROOT.parent / "data" / "profiles.json"))
LIVE = Path(os.getenv("LIVE_PATH", ROOT.parent / "data" / "live.json"))
STATE = LIVE.parent / "state.json"  # сканы и чаты

app = Flask(__name__, static_folder=str(ROOT / "static"))
# Единственный аккаунт организатора создаётся заранее через переменные окружения
ORG_LOGIN = os.getenv("ORG_LOGIN", "organizer")
ORG_PASSWORD = os.getenv("ORG_PASSWORD", "tanystap-2026")
app.secret_key = os.getenv("SECRET_KEY") or hashlib.sha256(("tanystap:" + ORG_PASSWORD).encode()).hexdigest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
rec = Recommender(DATA, LIVE)
SESSIONS: dict[str, dict] = {}
LOCK = threading.RLock()  # повторный вход: боты сохраняют анкеты внутри общей блокировки

NETWORKS = {
    "linkedin": ("linkedin.com", "https://www.linkedin.com/in/"),
    "facebook": ("facebook.com", "https://www.facebook.com/"),
    "tiktok": ("tiktok.com", "https://www.tiktok.com/@"),
    "instagram": ("instagram.com", "https://www.instagram.com/"),
    "telegram": ("t.me", "https://t.me/"),
}
CONTACT_KEYS = {"whatsapp", "phone", "email"}
SHOW_KEYS = set(NETWORKS) | CONTACT_KEYS | {"avatar", "compat", "background"}
PHONE_ERR = "Номер телефона должен начинаться с +7 и содержать 11 цифр, например +7 701 123 45 67"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}")


def kz_phone(v) -> str | None:
    """Только казахстанский формат: +7 и 10 цифр. Возвращает +7XXXXXXXXXX или None."""
    v = str(v or "").strip()
    for pre in ("tel:", "https://wa.me/", "http://wa.me/", "wa.me/"):
        if v.startswith(pre):
            v = v[len(pre):]
            if not v.startswith("+"):
                v = "+" + v
    if not v.startswith("+7"):
        return None
    digits = re.sub(r"[\s\-()]", "", v)
    return digits if re.fullmatch(r"\+7\d{10}", digits) else None


def contact_errors(raw) -> str | None:
    raw = raw or {}
    for k in ("whatsapp", "phone"):
        if str(raw.get(k) or "").strip() and not kz_phone(raw.get(k)):
            return PHONE_ERR
    e = str(raw.get("email") or "").strip().removeprefix("mailto:")
    if e and not EMAIL_RE.fullmatch(e):
        return "Проверьте email: например name@mail.kz"
    return None
DEFAULT_SHOW = ["avatar", "compat", "background"]
ROLE_LABEL = {"job": "как работник", "hire": "как работодатель", "find_mentor": "как ученик",
              "mentor": "как ментор", "cofounder": "как кофаундер", "exchange": "как собеседник по опыту"}
PRIVATE_KEYS = ("socials", "show", "hide")


def load_state():
    d = {}
    if STATE.exists():
        try:
            d = json.loads(STATE.read_text(encoding="utf-8"))
        except ValueError:
            d = {}
    return ({tuple(x) for x in d.get("scans", [])}, d.get("chats", {}), d.get("scan_log", []),
            d.get("read", {}), d.get("seen", {}), d.get("met", []), d.get("accounts", {}),
            d.get("bots", {"on": False, "sids": []}))


SCANS, CHATS, SCAN_LOG, READ, SEEN, MET, ACCOUNTS, BOTS = load_state()
# MET: отметки «познакомились»; ACCOUNTS: логин гостя -> {salt, hash, sid}
ACC_BY_SID = {v["sid"]: k for k, v in ACCOUNTS.items()}
# SCAN_LOG: кто кого тапнул и когда; READ[me][peer]: до какого времени прочитан чат; SEEN[me]: когда открывал уведомления


def save_state():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"scans": [list(x) for x in SCANS], "chats": CHATS, "scan_log": SCAN_LOG[-5000:],
                                 "read": READ, "seen": SEEN, "met": MET[-5000:],
                                 "accounts": ACCOUNTS, "bots": BOTS}, ensure_ascii=False), encoding="utf-8")


def own_id(sid: str) -> str:
    """Постоянный id бейджа участника, привязанный к его сессии в браузере."""
    return "u" + hashlib.sha1(sid.encode()).hexdigest()[:10]


def session(sid: str) -> dict:
    s = SESSIONS.setdefault(sid, {"exclude": set(), "met": set(), "penalties": {}, "query": None, "lang": "ru"})
    if not s["query"]:  # после перезапуска сервера берём сохранённую анкету
        pid = own_id(sid)
        if pid in rec.by_id:
            s["query"] = dict(rec.profiles[rec.by_id[pid]])
    return s


def clean_socials(raw) -> dict:
    """Принимаем только ссылки на нужные сети или ники. Всё остальное отбрасываем."""
    out = {}
    raw = raw or {}
    for key, (domain, base) in NETWORKS.items():
        v = str(raw.get(key) or "").strip()
        if not v:
            continue
        if v.startswith("http"):
            u = urlparse(v)
            host = u.netloc.lower()
            if u.scheme in ("http", "https") and (host == domain or host.endswith("." + domain)):
                out[key] = v
        else:
            handle = v.lstrip("@/").split("/")[-1]
            if re.fullmatch(r"[A-Za-z0-9._-]{1,60}", handle):
                out[key] = base + handle
    wa = kz_phone(raw.get("whatsapp"))
    if wa:
        out["whatsapp"] = "https://wa.me/" + wa[1:]
    ph = kz_phone(raw.get("phone"))
    if ph:
        out["phone"] = "tel:" + ph
    em = str(raw.get("email") or "").strip().removeprefix("mailto:")
    if em and EMAIL_RE.fullmatch(em):
        out["email"] = "mailto:" + em
    return out


def clean_avatar(v) -> str:
    v = str(v or "")
    if v and len(v) < 120000 and re.fullmatch(r"data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+", v):
        return v
    return ""


def clean_show(v) -> list:
    return [k for k in (v or []) if k in SHOW_KEYS]


def clean_hide(v) -> list:
    return [k for k in (v or []) if k in NETWORKS or k in CONTACT_KEYS]


def public_card(p: dict) -> dict:
    """То, что видит другой участник: только то, что владелец отметил для показа."""
    shown = set(p.get("show", DEFAULT_SHOW))
    background = None
    if "background" in shown:
        background = {k: p.get(k) for k in ("experience", "company", "stack", "interests", "about")}
    return {
        "id": p["id"],
        "name": p["name"],
        "role": p.get("role", ""),
        "company": p.get("company", ""),
        "avatar": p.get("avatar", "") if "avatar" in shown else "",
        # заполненная соцсеть видна всем, пока владелец её не скрыл
        "socials": {k: v for k, v in (p.get("socials") or {}).items() if k not in set(p.get("hide") or [])},
        "background": background,
        "live": bool(p.get("live")),
        "bot": bool(p.get("bot")),
    }


def own_view(p: dict) -> dict:
    return {k: v for k, v in p.items() if not k.startswith("_")}


def save_profile(sid: str, prof: dict) -> dict:
    pid = own_id(sid)
    prev = rec.profiles[rec.by_id[pid]] if pid in rec.by_id else {}
    merged = {k: v for k, v in prev.items() if not k.startswith("_")}
    merged.update(prof)  # поля, которых нет в форме (слоты, компания), не теряются
    prof = merged
    prof["id"] = pid
    prof["experience"] = int(prof.get("experience") or 0)
    prev = rec.profiles[rec.by_id[prof["id"]]] if prof["id"] in rec.by_id else {}
    prof["socials"] = clean_socials(prof["socials"]) if "socials" in prof else prev.get("socials", {})
    prof["show"] = clean_show(prof["show"]) if "show" in prof else prev.get("show", DEFAULT_SHOW)
    prof["hide"] = clean_hide(prof.get("hide"))
    prof["avatar"] = clean_avatar(prof["avatar"]) if "avatar" in prof else prev.get("avatar", "")
    prof.setdefault("stack", [])
    prof.setdefault("interests", [])
    acc = ACCOUNTS.get(ACC_BY_SID.get(sid, ""))
    if acc and not acc.get("contacts_added"):  # телефон или почта из регистрации сразу попадают в контакты
        socials = dict(prof.get("socials") or {})
        if acc.get("phone"):
            socials.setdefault("phone", "tel:" + acc["phone"])
            socials.setdefault("whatsapp", "https://wa.me/" + acc["phone"][1:])
        if acc.get("email"):
            socials.setdefault("email", "mailto:" + acc["email"])
        prof["socials"] = socials
        acc["contacts_added"] = True
    with LOCK:
        saved = rec.upsert(prof)
    return saved


def chat_allowed(me: str, pid: str) -> bool:
    """Чат открыт, если оба живые и хотя бы один сканировал другого."""
    if me == pid or me not in rec.by_id or pid not in rec.by_id:
        return False
    if not (rec.profiles[rec.by_id[me]].get("live") and rec.profiles[rec.by_id[pid]].get("live")):
        return False
    return (me, pid) in SCANS or (pid, me) in SCANS


def contacts_for(me: str) -> list:
    peers = {b for a, b in SCANS if a == me} | {a for a, b in SCANS if b == me}
    out = []
    for pid in peers:
        if pid in rec.by_id:
            card = public_card(rec.profiles[rec.by_id[pid]])
            card["chat"] = chat_allowed(me, pid)
            out.append(card)
    return sorted(out, key=lambda c: c["name"])


def strip_results(results: list) -> None:
    for r in results:
        p = r["profile"]
        avatar = public_card(p)["avatar"]
        r["profile"] = {k: v for k, v in p.items() if k not in PRIVATE_KEYS and k != "avatar"}
        r["profile"]["avatar"] = avatar


def assign_slots(q: dict, results: list[dict]) -> None:
    """Расписание 1:1: каждой рекомендации — свободный общий слот, без пересечений."""
    mine = q.get("slots") or []
    taken = set()
    for r in results:
        common = [s for s in mine if s in r["profile"].get("slots", []) and s not in taken]
        r["meeting_slot"] = common[0] if common else None
        if common:
            taken.add(common[0])


def run(sid: str) -> dict:
    s = session(sid)
    t0 = time.perf_counter()
    q, results = rec.recommend(s["query"], k=5, exclude=s["exclude"] | s["met"], penalties=s["penalties"])
    explain_all(q, results, s["lang"])
    assign_slots(q, results)
    strip_results(results)
    me = own_id(sid)
    return {
        "results": results,
        "met": len(s["met"]),
        "met_list": contacts_for(me),
        "me": {"id": me, "url": f"/p/{me}"},
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


@app.post("/api/recommend")
def recommend():
    body = request.get_json(force=True)
    sid = body.get("session_id") or "anon"
    prof = dict(body.get("profile") or {})
    if not str(prof.get("name", "")).strip() or prof.get("goal") not in GOAL_MATRIX:
        return jsonify({"error": "Заполните имя и цель"}), 400
    saved = save_profile(sid, prof)
    s = session(sid)
    s.update(query=dict(saved), lang=body.get("lang", "ru"), exclude=set(), penalties={})
    return jsonify(run(sid))


@app.post("/api/profile")
def profile_save():
    body = request.get_json(force=True)
    sid = body.get("session_id") or "anon"
    prof = dict(body.get("profile") or {})
    err = contact_errors(prof.get("socials"))
    if err:
        return jsonify({"error": err}), 400
    if not str(prof.get("name", "")).strip() or prof.get("goal") not in GOAL_MATRIX:
        return jsonify({"error": "Заполните имя и цель"}), 400
    saved = save_profile(sid, prof)
    s = session(sid)
    if s["query"]:
        s["query"] = dict(saved)
    return jsonify({"ok": True, "profile": own_view(saved), "card": public_card(saved)})


@app.get("/api/me")
def me_profile():
    sid = request.args.get("session_id", "")
    mid = own_id(sid)
    login = ACC_BY_SID.get(sid)
    if mid not in rec.by_id:
        return jsonify({"exists": False, "account": bool(login), "login": login})
    return jsonify({"exists": True, "account": bool(login), "login": login,
                    "profile": own_view(rec.profiles[rec.by_id[mid]])})


# ---------- аккаунты гостей ----------
def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def _clean_login(v) -> str:
    return re.sub(r"\s+", "", str(v or "")).lower()


@app.post("/api/register")
def register():
    body = request.get_json(force=True)
    raw_login, password = str(body.get("login") or "").strip(), str(body.get("password") or "")
    phone, email = None, None
    if "@" in raw_login:
        if not EMAIL_RE.fullmatch(raw_login):
            return jsonify({"error": "Проверьте email: например name@mail.kz"}), 400
        email = raw_login.lower()
        login = email
    else:
        phone = kz_phone(raw_login)
        if not phone:
            return jsonify({"error": PHONE_ERR}), 400
        login = phone
    if len(password) < 4:
        return jsonify({"error": "Пароль минимум 4 символа"}), 400
    with LOCK:
        if login in ACCOUNTS:
            return jsonify({"error": "Такой логин уже занят. Войдите или выберите другой."}), 409
        old = str(body.get("session_id") or "")
        # если в этом браузере уже была анкета без аккаунта — привязываем её
        sid = old if re.fullmatch(r"[a-z0-9]{6,32}", old) and old not in ACC_BY_SID else secrets.token_hex(8)
        salt = secrets.token_hex(8)
        ACCOUNTS[login] = {"salt": salt, "hash": _hash(password, salt), "sid": sid, "ts": int(time.time()),
                           "phone": phone, "email": email}
        ACC_BY_SID[sid] = login
        save_state()
    return jsonify({"ok": True, "session_id": sid, "has_profile": own_id(sid) in rec.by_id})


@app.post("/api/login")
def login_guest():
    body = request.get_json(force=True)
    raw = str(body.get("login") or "").strip()
    acc = ACCOUNTS.get(kz_phone(raw) or raw.lower())
    if not acc or not hmac.compare_digest(acc["hash"], _hash(str(body.get("password") or ""), acc["salt"])):
        return jsonify({"error": "Неверный логин или пароль"}), 401
    return jsonify({"ok": True, "session_id": acc["sid"], "has_profile": own_id(acc["sid"]) in rec.by_id})


GOAL_LABELS = {"hire": "Нанимают", "job": "Ищут работу", "mentor": "Готовы менторить", "find_mentor": "Ищут ментора",
               "cofounder": "Ищут кофаундера", "exchange": "Обмен опытом"}


PARTNERS_PATH = Path(os.getenv("PARTNERS_PATH", ROOT.parent / "data" / "partners.json"))
PARTNERS = json.loads(PARTNERS_PATH.read_text(encoding="utf-8")) if PARTNERS_PATH.exists() else []


@app.get("/api/partners")
def partners():
    """Стенды партнёров под цель и навыки участника: совпадение цели + пересечение тегов со стеком и интересами."""
    sid = request.args.get("session_id", "")
    me = own_id(sid)
    lang = request.args.get("lang", "ru")
    if me not in rec.by_id:
        return jsonify({"booths": []})
    p = rec.profiles[rec.by_id[me]]
    words = " ".join([p.get("role", ""), " ".join(p.get("stack", [])), " ".join(p.get("interests", [])), p.get("about", "")]).lower()
    scored = []
    for b in PARTNERS:
        hits = [t for t in b["tags"] if t in words]
        goal = p["goal"] in b["goals"]
        score = (0.5 if goal else 0) + min(len(hits), 4) * 0.125
        if score > 0:
            scored.append({"id": b["id"], "name": b["name"], "about": b["about"].get(lang) or b["about"]["ru"],
                           "zone": b["zone"], "goal_fit": goal, "tags": hits[:4], "score": round(score, 2)})
    scored.sort(key=lambda x: -x["score"])
    return jsonify({"booths": scored[:3]})


@app.get("/api/people")
def people():
    """Живые участники для меню и списка людей; совместимость считается с вами."""
    sid = request.args.get("session_id", "")
    me = own_id(sid)
    goal = request.args.get("goal")
    live = [p for p in rec.profiles if p.get("live") and p["id"] != me]
    counts = {g: sum(1 for p in live if p["goal"] == g) for g in GOAL_LABELS}
    out = []
    if goal in GOAL_LABELS:
        q = rec.profiles[rec.by_id[me]] if me in rec.by_id else None
        for p in live:
            if p["goal"] != goal:
                continue
            card = public_card(p)
            card["goal"] = p["goal"]
            card["score"] = rec.pair(q, p["id"])[1]["score"] if q else None
            card["chat"] = chat_allowed(me, p["id"])
            out.append(card)
        out.sort(key=lambda c: -(c["score"] or 0))
    return jsonify({"labels": GOAL_LABELS, "counts": counts, "people": out})


# ---------- организатор ----------
@app.post("/api/org/login")
def org_login():
    body = request.get_json(force=True)
    ok = hmac.compare_digest(str(body.get("login") or "").strip().lower(), ORG_LOGIN.lower()) and \
        hmac.compare_digest(str(body.get("password") or ""), ORG_PASSWORD)
    if not ok:
        time.sleep(0.5)
        return jsonify({"error": "Неверный логин или пароль организатора"}), 401
    web_session["org"] = True
    web_session.permanent = True
    return jsonify({"ok": True})


@app.post("/api/org/logout")
def org_logout():
    web_session.pop("org", None)
    return jsonify({"ok": True})


@app.get("/api/org/overview")
def org_overview():
    """Всё для кабинета организатора: участники, тапы, знакомства, чаты и потенциальные пары."""
    if not web_session.get("org"):
        return jsonify({"error": "organizer only"}), 403
    demo = request.args.get("demo") == "1"
    nodes = [p for p in rec.profiles if p.get("live")]
    if demo:  # демо-участники из синтетической базы, только с потенциальными связями
        base = [p for p in rec.profiles if not p.get("live")]
        nodes += base[::max(1, len(base) // 40)][:40]
    ids = [p["id"] for p in nodes]
    pos = {pid: i for i, pid in enumerate(ids)}
    m = rec.mutual_matrix(ids)

    edges, connected = [], set()
    pairs = {}
    for a, b in SCANS:
        if a in pos and b in pos and a != b:
            k = tuple(sorted((a, b)))
            pairs.setdefault(k, set()).add(a)
    for (a, b), who in pairs.items():
        ts = max([e["ts"] for e in SCAN_LOG if {e["from"], e["to"]} == {a, b}] or [0])
        edges.append({"a": a, "b": b, "type": "mutual" if len(who) == 2 else "tap",
                      "from": next(iter(who)), "ts": ts, "score": round(float(m[pos[a], pos[b]]), 2)})
        connected.add((a, b))
    for e in MET:
        if e["from"] in pos and e["to"] in pos:
            k = tuple(sorted((e["from"], e["to"])))
            edges.append({"a": k[0], "b": k[1], "type": "met", "from": e["from"], "ts": e["ts"],
                          "score": round(float(m[pos[k[0]], pos[k[1]]]), 2)})
            connected.add(k)
    msg_count = {}
    for key, msgs in CHATS.items():
        a, b = key.split("|")
        if a in pos and b in pos and msgs:
            msg_count[a] = msg_count.get(a, 0) + len(msgs)
            msg_count[b] = msg_count.get(b, 0) + len(msgs)
            edges.append({"a": a, "b": b, "type": "chat", "count": len(msgs), "ts": msgs[-1]["ts"],
                          "score": round(float(m[pos[a], pos[b]]), 2)})
            connected.add((a, b) if a < b else (b, a))
    # потенциальные пары: топ-3 для каждого, кто ещё не связан
    potential, seen_pairs = [], set()
    for i, pid in enumerate(ids):
        order = sorted(range(len(ids)), key=lambda j: -m[i, j])
        picked = 0
        for j in order:
            if j == i or picked >= 3 or m[i, j] < 0.3:
                continue
            k = tuple(sorted((pid, ids[j])))
            picked += 1
            if k in connected or k in seen_pairs:
                continue
            seen_pairs.add(k)
            potential.append({"a": k[0], "b": k[1], "type": "potential", "score": round(float(m[i, j]), 2)})
    potential.sort(key=lambda e: -e["score"])
    edges += potential

    taps_out = {pid: sum(1 for a, b in SCANS if a == pid and b in pos) for pid in ids}
    taps_in = {pid: sum(1 for a, b in SCANS if b == pid and a in pos) for pid in ids}
    met_n = {pid: sum(1 for e in MET if pid in (e["from"], e["to"])) for pid in ids}
    logins = {own_id(sid): login for sid, login in ACC_BY_SID.items()}
    reg_ts = {own_id(a["sid"]): a.get("ts", 0) for a in ACCOUNTS.values()}
    people_out = []
    for p in nodes:
        i = pos[p["id"]]
        best = max((j for j in range(len(ids)) if j != i), key=lambda j: m[i, j], default=None)
        people_out.append({
            "id": p["id"], "name": p["name"], "role": p.get("role", ""), "company": p.get("company", ""),
            "goal": p["goal"], "family": p.get("role_family", ""), "live": bool(p.get("live")), "bot": bool(p.get("bot")),
            "experience": p.get("experience", 0), "stack": p.get("stack", [])[:6], "interests": p.get("interests", [])[:6],
            "avatar": public_card(p)["avatar"], "login": logins.get(p["id"], ""), "registered": reg_ts.get(p["id"], 0),
            "taps_out": taps_out[p["id"]], "taps_in": taps_in[p["id"]], "met": met_n[p["id"]],
            "messages": msg_count.get(p["id"], 0),
            "best": {"id": ids[best], "name": nodes[best]["name"], "score": round(float(m[i, best]), 2)} if best is not None else None,
        })
    live_n = sum(1 for p in nodes if p.get("live") and not p.get("bot"))
    return jsonify({
        "people": people_out, "edges": edges, "goal_labels": GOAL_LABELS,
        "bots": {"on": BOTS["on"], "count": len(BOTS["sids"])},
        "kpi": {
            "participants": live_n,
            "taps": sum(1 for e in SCAN_LOG if e["from"] in pos and e["to"] in pos),
            "connections": len(pairs),
            "mutual": sum(1 for w in pairs.values() if len(w) == 2),
            "met": len({tuple(sorted((e["from"], e["to"]))) for e in MET if e["from"] in pos and e["to"] in pos}),
            "messages": sum(len(v) for k, v in CHATS.items() if all(x in pos for x in k.split("|"))),
            "potential": len(potential),
            "isolated": sum(1 for p in nodes if p.get("live") and not p.get("bot") and not taps_out[p["id"]] and not taps_in[p["id"]]),
            "bots": sum(1 for p in nodes if p.get("bot")),
        },
    })


@app.post("/api/feedback")
def feedback():
    body = request.get_json(force=True)
    sid = body["session_id"]
    s = session(sid)
    if not s["query"]:
        return jsonify({"error": "no query"}), 400
    if body["target_id"] not in rec.by_id:
        return jsonify({"error": "unknown"}), 404
    target = rec.profiles[rec.by_id[body["target_id"]]]
    if body["verdict"] == "met":
        s["met"].add(target["id"])
        me = own_id(sid)
        with LOCK:
            if not any(m["from"] == me and m["to"] == target["id"] for m in MET):
                MET.append({"from": me, "to": target["id"], "ts": int(time.time())})
                save_state()
    else:
        s["exclude"].add(target["id"])
        reason = body.get("reason")
        if reason == "goal":
            key = f"goal:{target['goal']}"
            s["penalties"][key] = s["penalties"].get(key, 1.0) * 0.6
        elif reason == "role":
            key = f"family:{target['role_family']}"
            s["penalties"][key] = s["penalties"].get(key, 1.0) * 0.7
    return jsonify(run(sid))


@app.post("/api/pair")
def pair():
    """NFC/QR: участник приложил телефон к бейджу."""
    body = request.get_json(force=True)
    sid = body.get("session_id") or "anon"
    s = session(sid)
    me = own_id(sid)
    tid = body.get("target_id", "")
    if tid == me:
        return jsonify({"own": True})
    if not s["query"]:
        return jsonify({"need_profile": True})
    if tid not in rec.by_id:
        return jsonify({"error": "unknown badge"}), 404
    q, r = rec.pair(s["query"], tid)
    explain_all(q, [r], body.get("lang") or s["lang"])
    assign_slots(q, [r])
    card = public_card(rec.profiles[rec.by_id[tid]])
    strip_results([r])
    with LOCK:
        if body.get("source") != "browse":  # просмотр из списка людей — это ещё не тап
            SCANS.add((me, tid))
        if body.get("source") not in ("view", "browse"):
            recent = any(e["from"] == me and e["to"] == tid and time.time() - e["ts"] < 600 for e in SCAN_LOG[-300:])
            if not recent:
                SCAN_LOG.append({"from": me, "to": tid, "ts": int(time.time())})
        save_state()
    tprof = rec.profiles[rec.by_id[tid]]
    if "compat" in set(tprof.get("show", DEFAULT_SHOW)):
        r["compat"] = {
            "them": {"score": round(r["for_you"] * 100), "label": ROLE_LABEL.get(tprof["goal"], "")},
            "you": {"score": round(r["for_them"] * 100), "label": ROLE_LABEL.get(q["goal"], "")},
        }
    r["card"] = card
    r["chat"] = chat_allowed(me, tid)
    return jsonify(r)


@app.get("/api/contacts")
def contacts():
    return jsonify({"contacts": contacts_for(own_id(request.args.get("session_id", "")))})


@app.get("/api/chat/<pid>")
def chat_get(pid):
    me = own_id(request.args.get("session_id", ""))
    if not chat_allowed(me, pid):
        return jsonify({"error": "forbidden"}), 403
    key = "|".join(sorted([me, pid]))
    with LOCK:
        READ.setdefault(me, {})[pid] = int(time.time())
        save_state()
    return jsonify({
        "me": me,
        "peer": public_card(rec.profiles[rec.by_id[pid]]),
        "messages": CHATS.get(key, [])[-200:],
    })


@app.post("/api/chat/<pid>")
def chat_post(pid):
    body = request.get_json(force=True)
    me = own_id(body.get("session_id", ""))
    if not chat_allowed(me, pid):
        return jsonify({"error": "forbidden"}), 403
    text = str(body.get("text", "")).strip()
    if not text or len(text) > 500:
        return jsonify({"error": "Сообщение от 1 до 500 символов"}), 400
    key = "|".join(sorted([me, pid]))
    with LOCK:
        msgs = CHATS.setdefault(key, [])
        msgs.append({"from": me, "text": text, "ts": int(time.time())})
        CHATS[key] = msgs[-500:]
        save_state()
    return jsonify({"messages": CHATS[key][-200:]})


def card_of(pid: str) -> dict | None:
    return public_card(rec.profiles[rec.by_id[pid]]) if pid in rec.by_id else None


def unread(me: str, pid: str) -> int:
    since = READ.get(me, {}).get(pid, 0)
    msgs = CHATS.get("|".join(sorted([me, pid])), [])
    return sum(1 for m in msgs if m["from"] != me and m["ts"] > since)


def inbox_for(me: str) -> list:
    """Все, с кем есть связь через тап: последние сообщения и непрочитанные."""
    out = []
    peers = {b for a, b in SCANS if a == me} | {a for a, b in SCANS if b == me}
    for pid in peers:
        card = card_of(pid)
        if not card or pid == me:
            continue
        msgs = CHATS.get("|".join(sorted([me, pid])), [])
        last = msgs[-1] if msgs else None
        taps = [e["ts"] for e in SCAN_LOG if {e["from"], e["to"]} == {me, pid}]
        out.append({
            "card": card,
            "chat": chat_allowed(me, pid),
            "i_tapped": (me, pid) in SCANS,
            "tapped_me": (pid, me) in SCANS,
            "last": {"text": last["text"], "mine": last["from"] == me, "ts": last["ts"]} if last else None,
            "unread": unread(me, pid),
            "ts": max([last["ts"] if last else 0] + taps),
        })
    return sorted(out, key=lambda x: -x["ts"])


def notifications_for(me: str) -> list:
    ev = []
    for e in SCAN_LOG:
        if e["to"] == me and e["from"] != me and e["from"] in rec.by_id:
            ev.append({"type": "tap", "ts": e["ts"], "card": card_of(e["from"])})
    for key, msgs in CHATS.items():
        if me not in key.split("|"):
            continue
        for m in msgs:
            if m["from"] != me and m["from"] in rec.by_id:
                ev.append({"type": "message", "ts": m["ts"], "text": m["text"][:120], "card": card_of(m["from"])})
    ev.sort(key=lambda x: -x["ts"])
    return ev[:100]


@app.get("/api/inbox")
def inbox():
    me = own_id(request.args.get("session_id", ""))
    return jsonify({"me": me, "threads": inbox_for(me)})


@app.get("/api/notifications")
def notifications():
    me = own_id(request.args.get("session_id", ""))
    seen = SEEN.get(me, 0)
    ev = notifications_for(me)
    for e in ev:
        e["new"] = e["ts"] > seen
    if request.args.get("mark") == "1":
        with LOCK:
            SEEN[me] = int(time.time())
            save_state()
    return jsonify({"events": ev})


@app.get("/api/counts")
def counts():
    """Цифры на значках меню: непрочитанные сообщения и новые уведомления."""
    me = own_id(request.args.get("session_id", ""))
    peers = {b for a, b in SCANS if a == me} | {a for a, b in SCANS if b == me}
    seen = SEEN.get(me, 0)
    return jsonify({
        "messages": sum(unread(me, p) for p in peers),
        "notifications": sum(1 for e in notifications_for(me) if e["ts"] > seen),
        "has_profile": me in rec.by_id,
    })


@app.get("/api/stats")
def stats():
    if not web_session.get("org"):
        return jsonify({"error": "organizer only"}), 403
    d = rec.stats()
    d["met_total"] = sum(len(v["met"]) for v in SESSIONS.values())
    d["live"] = sum(1 for p in rec.profiles if p.get("live"))
    d["scans_total"] = len(SCANS)
    d["chats_total"] = sum(1 for v in CHATS.values() if v)
    return jsonify(d)


@app.get("/api/health")
def health():
    return {"ok": True, "profiles": len(rec.profiles)}


@app.get("/")
def landing():
    return send_from_directory(app.static_folder, "landing.html")


@app.get("/app")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/join")
def join_page():
    return send_from_directory(app.static_folder, "join.html")


@app.get("/people")
def people_page():
    return send_from_directory(app.static_folder, "people.html")


@app.get("/org")
def org_page():
    if web_session.get("org"):
        return redirect("/dashboard")
    return send_from_directory(app.static_folder, "org_login.html")


@app.get("/dashboard")
def dashboard():
    if not web_session.get("org"):
        return redirect("/org")
    return send_from_directory(app.static_folder, "dashboard.html")


@app.get("/profile")
def profile_page():
    return send_from_directory(app.static_folder, "profile.html")


@app.get("/p/<pid>")
def badge(pid):
    return send_from_directory(app.static_folder, "badge.html")


@app.get("/api/qr")
def qr_image():
    """QR-код для ссылки бейджа, рисуется на сервере. Ссылка уходит только на наш сайт."""
    data = request.args.get("data", "")
    if not data.startswith(("http://", "https://")) or len(data) > 300:
        return jsonify({"error": "bad data"}), 400
    try:
        import io
        import qrcode
    except ImportError:
        return jsonify({"error": "qr library missing"}), 503
    buf = io.BytesIO()
    qrcode.make(data, box_size=8, border=2).save(buf, format="PNG")
    resp = app.response_class(buf.getvalue(), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/setup")
def setup_page():
    return send_from_directory(app.static_folder, "setup.html")


@app.get("/messages")
def messages_page():
    return send_from_directory(app.static_folder, "messages.html")


@app.get("/notifications")
def notifications_page():
    return send_from_directory(app.static_folder, "notifications.html")


@app.get("/chat/<pid>")
def chat_page(pid):
    return send_from_directory(app.static_folder, "chat.html")


@app.get("/badges")
def badges():
    """Список ссылок для записи на NFC-метки или печати QR на бейджах."""
    base = request.host_url.rstrip("/")
    rows = "".join(f"<tr><td>{p['id']}</td><td>{p['name']}</td><td>{p['role']}</td>"
                   f"<td><a href='/p/{p['id']}'>{base}/p/{p['id']}</a></td></tr>" for p in rec.profiles)
    return (f"<meta charset='utf-8'><title>Бейджи</title><style>body{{font-family:sans-serif;padding:20px}}"
            f"td{{padding:4px 10px;border-bottom:1px solid #eee}}</style><h2>Ссылки для NFC-меток / QR на бейджах</h2>"
            f"<table>{rows}</table>")


# ---------- боты-участники ----------
# Демонстрационные участники, которые сами тапают бейджи, переписываются и знакомятся.
# Везде помечены как «бот», чтобы живые люди понимали, с кем говорят.
import random

BOT_TICK = float(os.getenv("BOT_TICK", "6"))
BOT_OPEN = ["Привет! Я {me}, {role}. Вижу, у нас совместимость {pct}% — давай познакомимся?",
            "Здравствуйте! Мне подсказали вас как хорошее знакомство: {why}. Есть 10 минут поговорить?",
            "Привет! {common}Можем пересечься у зоны «{zone}»?"]
BOT_REPLY = ["Да, давай! Подойду через 10 минут.", "С удовольствием. Где ты сейчас?",
             "Отлично, давай в перерыве у кофе-зоны.", "Привет! Да, интересно, расскажи подробнее."]
BOT_HUMAN = ["Спасибо, что написали! Я бот-участник TanysTap — показываю, как работает сервис. "
             "Живые люди, которые вам подходят, — в разделе «Подбор» и в меню ⋮.",
             "Это демо-бот. Попробуйте тапнуть бейдж живого участника: так откроется настоящий чат."]


def bot_ids() -> list[str]:
    return [own_id(s) for s in BOTS["sids"] if own_id(s) in rec.by_id]


def create_bots(count: int) -> int:
    """Берём анкеты из синтетической базы с разными целями и делаем из них живых ботов."""
    count = max(2, min(int(count or 12), 30))
    base = [p for p in rec.profiles if not p.get("live")]
    by_goal = {}
    for p in base:
        by_goal.setdefault(p["goal"], []).append(p)
    rnd = random.Random(len(BOTS["sids"]) + int(time.time()))
    picked = []
    while len(picked) < count and any(by_goal.values()):
        for g in list(by_goal):
            if by_goal[g] and len(picked) < count:
                picked.append(by_goal[g].pop(rnd.randrange(len(by_goal[g]))))
    for src in picked:
        sid = "bot" + secrets.token_hex(6)
        prof = {k: v for k, v in src.items() if not k.startswith("_") and k not in ("id", "live")}
        prof.update(bot=True, show=DEFAULT_SHOW, hide=[], socials={}, avatar="")
        BOTS["sids"].append(sid)
        save_profile(sid, prof)
    return len(picked)


def remove_bots() -> None:
    ids = set(bot_ids())
    global SCANS
    SCANS = {x for x in SCANS if x[0] not in ids and x[1] not in ids}
    SCAN_LOG[:] = [e for e in SCAN_LOG if e["from"] not in ids and e["to"] not in ids]
    MET[:] = [e for e in MET if e["from"] not in ids and e["to"] not in ids]
    for k in [k for k in CHATS if any(x in ids for x in k.split("|"))]:
        del CHATS[k]
    rec.remove(ids)
    BOTS["sids"] = []
    BOTS["on"] = False


def _send(frm: str, to: str, text: str) -> None:
    key = "|".join(sorted([frm, to]))
    CHATS.setdefault(key, []).append({"from": frm, "text": text, "ts": int(time.time())})


def bot_tick() -> None:
    ids = bot_ids()
    if not BOTS["on"] or len(ids) < 2:
        return
    now, idset = int(time.time()), set(ids)
    # 1. ответить живым людям, которые написали боту
    for key, msgs in CHATS.items():
        a, b = key.split("|")
        if not msgs or msgs[-1]["from"] in idset or now - msgs[-1]["ts"] < 3:
            continue
        bot = a if a in idset else b if b in idset else None
        if bot:
            n_bot = sum(1 for m in msgs if m["from"] == bot)
            _send(bot, msgs[-1]["from"], BOT_HUMAN[min(n_bot, 1)] if n_bot < 2 else random.choice(BOT_REPLY))
            return
    # 2. бот отвечает боту
    for key, msgs in CHATS.items():
        a, b = key.split("|")
        if a in idset and b in idset and msgs and len(msgs) < 4 and now - msgs[-1]["ts"] > BOT_TICK and random.random() < 0.6:
            last_from = msgs[-1]["from"]
            other = b if last_from == a else a
            _send(other, last_from, random.choice(BOT_REPLY))
            if len(msgs) >= 2 and random.random() < 0.7 and not any({e["from"], e["to"]} == {a, b} for e in MET):
                MET.append({"from": other, "to": last_from, "ts": now})
            return
    # 3. новый тап: бот выбирает подходящего человека (бота или живого)
    me = random.choice(ids)
    mp = rec.profiles[rec.by_id[me]]
    taps_by_me = sum(1 for x, _ in SCANS if x == me)
    if taps_by_me >= 5:
        return
    cands = []
    for p in rec.profiles:
        if not p.get("live") or p["id"] == me or (me, p["id"]) in SCANS:
            continue
        if not p.get("bot") and sum(1 for x, y in SCANS if y == p["id"] and x in idset) >= 2:
            continue  # живого человека боты тапают не больше двух раз
        cands.append(p)
    if not cands:
        return
    scored = [(rec.pair(mp, p["id"])[1], p) for p in cands]
    weights = [max(0.01, r["score"]) ** 3 for r, _ in scored]
    r, target = random.choices(scored, weights=weights, k=1)[0]
    SCANS.add((me, target["id"]))
    SCAN_LOG.append({"from": me, "to": target["id"], "ts": now})
    if target.get("bot") and random.random() < 0.75:
        common = ", ".join((r["common_stack"] + r["common_interests"])[:2])
        _send(me, target["id"], random.choice(BOT_OPEN).format(
            me=mp["name"].split()[0], role=mp.get("role", ""), pct=round(r["score"] * 100),
            why=("общий стек: " + common) if common else "у нас дополняющие цели",
            common=(f"У нас общее: {common}. ") if common else "", zone=GOAL_LABELS.get(target["goal"], "")))


def bot_loop() -> None:
    while True:
        time.sleep(BOT_TICK)
        try:
            if BOTS["on"]:
                with LOCK:
                    bot_tick()
                    save_state()
        except Exception as e:  # симуляция не должна ронять сервер
            print("bot tick error:", e)


threading.Thread(target=bot_loop, daemon=True).start()


@app.get("/dashboard/badge/<pid>")
def org_print_badge(pid):
    """Карточка бейджа для печати: имя, роль, QR и ссылка. Только для организатора."""
    if not web_session.get("org"):
        return redirect("/org")
    if pid not in rec.by_id:
        return "Участник не найден", 404
    p = rec.profiles[rec.by_id[pid]]
    import html
    url = request.host_url.rstrip("/") + "/p/" + pid
    name, role = html.escape(p["name"]), html.escape(p.get("role", ""))
    return (f"<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Бейдж — {name}</title><style>body{{font-family:Inter,system-ui,sans-serif;color:#0B1E3F;display:flex;justify-content:center;padding:30px}}"
            f".b{{width:300px;border:2px solid #0B1E3F;border-radius:18px;padding:24px;text-align:center}}h1{{font-size:24px;margin:8px 0 4px}}"
            f"p{{color:#5A6B8C;margin:0 0 14px}}img{{width:200px;height:200px}}small{{display:block;word-break:break-all;color:#5A6B8C;margin-top:8px}}"
            f".t{{font-weight:700;letter-spacing:.04em}}@media print{{button{{display:none}}}}</style></head><body><div class='b'>"
            f"<div class='t'>TanysTap</div><h1>{name}</h1><p>{role}</p><img src='/api/qr?data={html.escape(url)}' alt='QR'>"
            f"<small>{html.escape(url)}</small><button onclick='print()' style='margin-top:14px;padding:10px 16px;border:0;border-radius:8px;background:#0B1E3F;color:#fff;font-size:14px;cursor:pointer'>Печать</button>"
            f"</div></body></html>")


@app.post("/api/org/bots")
def org_bots():
    if not web_session.get("org"):
        return jsonify({"error": "organizer only"}), 403
    body = request.get_json(force=True)
    action = body.get("action")
    with LOCK:
        if action == "start":
            if not bot_ids():
                create_bots(body.get("count", 12))
            BOTS["on"] = True
        elif action == "pause":
            BOTS["on"] = False
        elif action == "remove":
            remove_bots()
        save_state()
    return jsonify({"on": BOTS["on"], "count": len(bot_ids())})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=False)
