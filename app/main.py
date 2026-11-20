import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory

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
rec = Recommender(DATA, LIVE)
SESSIONS: dict[str, dict] = {}
LOCK = threading.Lock()

NETWORKS = {
    "linkedin": ("linkedin.com", "https://www.linkedin.com/in/"),
    "facebook": ("facebook.com", "https://www.facebook.com/"),
    "tiktok": ("tiktok.com", "https://www.tiktok.com/@"),
    "instagram": ("instagram.com", "https://www.instagram.com/"),
    "telegram": ("t.me", "https://t.me/"),
}
SHOW_KEYS = set(NETWORKS) | {"whatsapp", "avatar", "compat", "background"}
DEFAULT_SHOW = ["avatar", "compat", "background"]
ROLE_LABEL = {"job": "как работник", "hire": "как работодатель", "find_mentor": "как ученик",
              "mentor": "как ментор", "cofounder": "как кофаундер", "exchange": "как собеседник по опыту"}
PRIVATE_KEYS = ("socials", "show")


def load_state():
    if STATE.exists():
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return {tuple(x) for x in d.get("scans", [])}, d.get("chats", {})
    return set(), {}


SCANS, CHATS = load_state()


def save_state():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"scans": [list(x) for x in SCANS], "chats": CHATS}, ensure_ascii=False), encoding="utf-8")


def own_id(sid: str) -> str:
    """Постоянный id бейджа участника, привязанный к его сессии в браузере."""
    return "u" + hashlib.sha1(sid.encode()).hexdigest()[:10]


def session(sid: str) -> dict:
    return SESSIONS.setdefault(sid, {"exclude": set(), "met": set(), "penalties": {}, "query": None, "lang": "ru"})


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
    digits = re.sub(r"\D", "", str(raw.get("whatsapp") or ""))
    if 7 <= len(digits) <= 15:
        out["whatsapp"] = "https://wa.me/" + digits
    return out


def clean_avatar(v) -> str:
    v = str(v or "")
    if v and len(v) < 120000 and re.fullmatch(r"data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+", v):
        return v
    return ""


def clean_show(v) -> list:
    return [k for k in (v or []) if k in SHOW_KEYS]


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
        "socials": {k: v for k, v in (p.get("socials") or {}).items() if k in shown},
        "background": background,
        "live": bool(p.get("live")),
    }


def own_view(p: dict) -> dict:
    return {k: v for k, v in p.items() if not k.startswith("_")}


def save_profile(sid: str, prof: dict) -> dict:
    prof = dict(prof)
    prof["id"] = own_id(sid)
    prof["experience"] = int(prof.get("experience") or 0)
    prev = rec.profiles[rec.by_id[prof["id"]]] if prof["id"] in rec.by_id else {}
    prof["socials"] = clean_socials(prof["socials"]) if "socials" in prof else prev.get("socials", {})
    prof["show"] = clean_show(prof["show"]) if "show" in prof else prev.get("show", DEFAULT_SHOW)
    prof["avatar"] = clean_avatar(prof["avatar"]) if "avatar" in prof else prev.get("avatar", "")
    prof.setdefault("stack", [])
    prof.setdefault("interests", [])
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
    if not str(prof.get("name", "")).strip() or prof.get("goal") not in GOAL_MATRIX:
        return jsonify({"error": "Заполните имя и цель"}), 400
    saved = save_profile(sid, prof)
    s = session(sid)
    if s["query"]:
        s["query"] = dict(saved)
    return jsonify({"ok": True, "profile": own_view(saved), "card": public_card(saved)})


@app.get("/api/me")
def me_profile():
    mid = own_id(request.args.get("session_id", ""))
    if mid not in rec.by_id:
        return jsonify({"exists": False})
    return jsonify({"exists": True, "profile": own_view(rec.profiles[rec.by_id[mid]])})


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
        SCANS.add((me, tid))
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


@app.get("/api/stats")
def stats():
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
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/dashboard")
def dashboard():
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
    return app.response_class(buf.getvalue(), mimetype="image/png")


@app.get("/setup")
def setup_page():
    return send_from_directory(app.static_folder, "setup.html")


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=False)
