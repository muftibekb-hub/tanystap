"""Гибридный рекомендатель: совместимость целей + текстовая близость + стек + опыт,
со взаимностью (скор в обе стороны) и разнообразием выдачи (MMR)."""
import json
import re
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

# Насколько профиль с целью B полезен человеку с целью A (строка A, столбец B)
GOAL_MATRIX = {
    "job":         {"job": 0.0,  "hire": 1.0, "find_mentor": 0.05, "mentor": 0.45, "cofounder": 0.2, "exchange": 0.3},
    "hire":        {"job": 1.0,  "hire": 0.1, "find_mentor": 0.35, "mentor": 0.2,  "cofounder": 0.1, "exchange": 0.3},
    "find_mentor": {"job": 0.05, "hire": 0.3, "find_mentor": 0.1,  "mentor": 1.0,  "cofounder": 0.1, "exchange": 0.35},
    "mentor":      {"job": 0.4,  "hire": 0.2, "find_mentor": 1.0,  "mentor": 0.2,  "cofounder": 0.2, "exchange": 0.3},
    "cofounder":   {"job": 0.15, "hire": 0.1, "find_mentor": 0.05, "mentor": 0.35, "cofounder": 1.0, "exchange": 0.3},
    "exchange":    {"job": 0.3,  "hire": 0.3, "find_mentor": 0.3,  "mentor": 0.5,  "cofounder": 0.3, "exchange": 1.0},
}
TECH = {"backend", "frontend", "mobile", "data", "devops", "analytics", "qa", "security"}

FAMILY_KEYWORDS = {
    "backend": ["backend", "бэкенд", "бекенд", "server", "api", "django", "fastapi", "spring", "go", "java", "node"],
    "frontend": ["frontend", "фронтенд", "react", "vue", "javascript", "typescript", "верстк"],
    "mobile": ["mobile", "мобиль", "android", "ios", "flutter", "kotlin", "swift"],
    "data": ["data scien", "ml", "machine learning", "машинн", "pytorch", "нейросет", "llm", "nlp", "ai"],
    "analytics": ["аналит", "analyst", "bi", "tableau", "power bi"],
    "devops": ["devops", "kubernetes", "sre", "terraform", "инфраструкт"],
    "qa": ["qa", "тестиров", "tester", "автотест"],
    "design": ["design", "дизайн", "ux", "ui", "figma"],
    "product": ["product", "продакт", "продукт", "pm", "менеджер"],
    "security": ["security", "безопасн", "pentest", "иб", "soc"],
    "business": ["бизнес", "business", "предприним", "founder", "основател", "продаж", "sales", "ceo"],
}


def guess_family(role: str, stack: list[str]) -> str:
    text = (role + " " + " ".join(stack)).lower()
    best, best_hits = "backend", 0
    for fam, kws in FAMILY_KEYWORDS.items():
        hits = sum(1 for k in kws if re.search(r"(^|[^a-zа-я])" + re.escape(k), text))
        if fam in role.lower():
            hits += 2
        if hits > best_hits:
            best, best_hits = fam, hits
    return best


def norm_set(items) -> set[str]:
    return {s.strip().lower() for s in items if s and s.strip()}


def profile_text(p: dict) -> str:
    return " ".join([p.get("role", ""), " ".join(p.get("stack", [])), " ".join(p.get("interests", [])), p.get("about", "")])


class Recommender:
    def __init__(self, path: Path, live_path: Path | None = None):
        self.live_path = Path(live_path) if live_path else None
        self.profiles: list[dict] = json.loads(Path(path).read_text(encoding="utf-8"))
        if self.live_path and self.live_path.exists():  # живые участники, сохранённые до перезапуска
            self.profiles += json.loads(self.live_path.read_text(encoding="utf-8"))
        for p in self.profiles:
            p.setdefault("role_family", guess_family(p["role"], p["stack"]))
            p["_stack"] = norm_set(p["stack"])
            p["_interests"] = norm_set(p["interests"])
        # Символьные n-граммы: устойчивы к казахскому/русскому/английскому и опечаткам
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, min_df=1)
        self.X = self.vec.fit_transform([profile_text(p) for p in self.profiles])
        self.by_id = {p["id"]: i for i, p in enumerate(self.profiles)}

    def upsert(self, p: dict) -> dict:
        """Добавить или обновить анкету живого участника (его бейдж)."""
        p = self.prepare_query(p)
        p["lang"] = p.get("lang", "ru")
        p.setdefault("company", "")
        p.setdefault("slots", [])
        p["live"] = True
        if p["id"] in self.by_id:
            self.profiles[self.by_id[p["id"]]] = p
        else:
            self.by_id[p["id"]] = len(self.profiles)
            self.profiles.append(p)
        self.X = self.vec.transform([profile_text(x) for x in self.profiles])
        if self.live_path:
            live = [x for x in self.profiles if x.get("live")]
            self.live_path.parent.mkdir(parents=True, exist_ok=True)
            clean = [{k: v for k, v in x.items() if not k.startswith("_")} for x in live]
            self.live_path.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
        return p

    # --- односторонний скор: насколько b полезен a ---
    def _one_way(self, a: dict, b: dict, text_sim: float) -> tuple[float, dict]:
        goal = GOAL_MATRIX[a["goal"]][b["goal"]]
        stack = len(a["_stack"] & b["_stack"]) / max(1, min(len(a["_stack"]), len(b["_stack"])))
        interests = len(a["_interests"] & b["_interests"]) / max(1, min(len(a["_interests"]), len(b["_interests"])))
        diff = b["experience"] - a["experience"]
        fam_same = a["role_family"] == b["role_family"]

        if a["goal"] == "find_mentor" and b["goal"] == "mentor":
            exp_fit = 1.0 if diff >= 3 else 0.3
        elif a["goal"] == "mentor" and b["goal"] == "find_mentor":
            exp_fit = 1.0 if diff <= -3 else 0.3
        elif a["goal"] == "exchange":
            exp_fit = 1.0 - min(abs(diff), 8) / 8
        else:
            exp_fit = 0.6

        # Найм: важно совпадение направления (бэкендеру — нанимающих бэкендеров)
        role_fit = 1.0 if fam_same else 0.25
        if {a["goal"], b["goal"]} == {"cofounder"}:
            a_tech, b_tech = a["role_family"] in TECH, b["role_family"] in TECH
            role_fit = 1.0 if a_tech != b_tech else (0.5 if not fam_same else 0.3)
        if a["goal"] in {"exchange", "find_mentor", "mentor"} and b["goal"] in {"exchange", "find_mentor", "mentor"}:
            role_fit = 1.0 if fam_same else 0.5

        score = 0.38 * goal + 0.2 * role_fit + 0.15 * text_sim + 0.12 * stack + 0.08 * interests + 0.07 * exp_fit
        return score, {"goal": goal, "role": role_fit, "text": text_sim, "stack": stack, "interests": interests, "experience": exp_fit}

    def prepare_query(self, q: dict) -> dict:
        q = dict(q)
        q["stack"] = [s.strip() for s in q.get("stack", []) if s.strip()]
        q["interests"] = [s.strip() for s in q.get("interests", []) if s.strip()]
        q["experience"] = int(q.get("experience") or 0)
        q["role_family"] = q.get("role_family") or guess_family(q.get("role", ""), q["stack"])
        q["_stack"] = norm_set(q["stack"])
        q["_interests"] = norm_set(q["interests"])
        return q

    def recommend(self, q: dict, k: int = 5, exclude: set[str] | None = None, penalties: dict | None = None):
        """penalties: {"goal:<g>": коэфф, "family:<f>": коэфф} — из фидбека «не подошло»."""
        q = self.prepare_query(q)
        exclude = exclude or set()
        penalties = penalties or {}
        qv = self.vec.transform([profile_text(q)])
        sims = (self.X @ qv.T).toarray().ravel()

        cands = []
        for i, b in enumerate(self.profiles):
            if b["id"] in exclude or b["id"] == q.get("id"):
                continue
            s_ab, parts = self._one_way(q, b, float(sims[i]))
            s_ba, _ = self._one_way(b, q, float(sims[i]))
            mutual = 2 * s_ab * s_ba / (s_ab + s_ba + 1e-9)  # гармоническое среднее — взаимность
            score = 0.7 * mutual + 0.3 * s_ab
            score *= penalties.get(f"goal:{b['goal']}", 1.0) * penalties.get(f"family:{b['role_family']}", 1.0)
            cands.append((score, i, parts, s_ab, s_ba))

        cands.sort(key=lambda c: c[0], reverse=True)
        pool = cands[:40]
        # MMR: не выдавать пять почти одинаковых людей
        chosen = []
        while pool and len(chosen) < k:
            best_j, best_val = 0, -1e9
            for j, c in enumerate(pool):
                redundancy = 0.0
                if chosen:
                    redundancy = max(float((self.X[c[1]] @ self.X[ch[1]].T).toarray()[0, 0]) for ch in chosen)
                val = c[0] - 0.12 * redundancy
                if val > best_val:
                    best_j, best_val = j, val
            chosen.append(pool.pop(best_j))

        results = []
        for score, i, parts, s_ab, s_ba in chosen:
            b = self.profiles[i]
            results.append({
                "profile": {k: v for k, v in b.items() if not k.startswith("_")},
                "score": round(score, 3),
                "for_you": round(s_ab, 3),
                "for_them": round(s_ba, 3),
                "breakdown": {k: round(v, 2) for k, v in parts.items()},
                "common_stack": sorted(set(b["stack"]) & set(q["stack"]), key=str.lower) or
                                [s for s in b["stack"] if s.lower() in q["_stack"]],
                "common_interests": [s for s in b["interests"] if s.lower() in q["_interests"]],
            })
        return q, results

    def pair(self, q: dict, target_id: str) -> tuple[dict, dict]:
        """Совместимость с одним конкретным человеком (для NFC/QR-бейджа)."""
        q = self.prepare_query(q)
        i = self.by_id[target_id]
        b = self.profiles[i]
        sim = float((self.X[i] @ self.vec.transform([profile_text(q)]).T).toarray()[0, 0])
        s_ab, parts = self._one_way(q, b, sim)
        s_ba, _ = self._one_way(b, q, sim)
        mutual = 2 * s_ab * s_ba / (s_ab + s_ba + 1e-9)
        return q, {
            "profile": {k: v for k, v in b.items() if not k.startswith("_")},
            "score": round(0.7 * mutual + 0.3 * s_ab, 3), "for_you": round(s_ab, 3), "for_them": round(s_ba, 3),
            "breakdown": {k: round(v, 2) for k, v in parts.items()},
            "common_stack": [x for x in b["stack"] if x.lower() in q["_stack"]],
            "common_interests": [x for x in b["interests"] if x.lower() in q["_interests"]],
        }

    def stats(self) -> dict:
        from collections import Counter
        from sklearn.decomposition import TruncatedSVD

        goals = Counter(p["goal"] for p in self.profiles)
        fams = Counter(p["role_family"] for p in self.profiles)
        interests = Counter(i for p in self.profiles for i in p["interests"])
        stack = Counter(s for p in self.profiles for s in p["stack"])
        coords = TruncatedSVD(n_components=2, random_state=0).fit_transform(self.X)
        coords = (coords - coords.min(0)) / (np.ptp(coords, 0) + 1e-9)
        points = [{"x": float(x), "y": float(y), "name": p["name"], "role": p["role"], "family": p["role_family"], "goal": p["goal"]}
                  for (x, y), p in zip(coords, self.profiles)]
        return {
            "total": len(self.profiles),
            "goals": dict(goals),
            "families": dict(fams.most_common()),
            "top_interests": interests.most_common(12),
            "top_stack": stack.most_common(12),
            "balance": [
                {"key": "job_vs_hire", "a": goals.get("job", 0), "b": goals.get("hire", 0)},
                {"key": "mentee_vs_mentor", "a": goals.get("find_mentor", 0), "b": goals.get("mentor", 0)},
            ],
            "points": points,
        }
