// Меню гостя: кнопка ⋮ в левом верхнем углу открывает панель с разделами, счётчиками и фото профиля.
// Здесь же проверка входа: без регистрации гость попадает на страницу /join.
(function () {
  const path = location.pathname;
  let sid = "";
  try { sid = localStorage.getItem("sid") || ""; } catch (e) {}
  const here = path + location.search;

  // ---------- проверка входа ----------
  if (!sid) { location.replace("/join?next=" + encodeURIComponent(here)); return; }

  const css = document.createElement("style");
  css.textContent = `
  .tt-left{display:flex;align-items:center;gap:6px;min-width:0}
  .tt-dots{width:40px;height:40px;border:0;background:none;border-radius:10px;cursor:pointer;display:flex;align-items:center;justify-content:center;position:relative;color:#0B1E3F;flex:none}
  .tt-dots:hover{background:#EAF0FA}
  .tt-dots svg{width:22px;height:22px}
  .tt-dots .tt-sum{position:absolute;top:3px;right:3px;min-width:16px;height:16px;padding:0 4px;border-radius:8px;background:#B42318;color:#fff;font-size:10px;font-weight:700;display:none;align-items:center;justify-content:center}
  .tt-bar{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid #DDE3EE;background:#fff}
  .tt-bar a{display:flex;align-items:center;gap:8px;font-weight:600;color:#0B1E3F;text-decoration:none;font-size:17px}
  .tt-shade{position:fixed;inset:0;background:rgba(11,30,63,.35);opacity:0;pointer-events:none;transition:opacity .2s;z-index:90}
  .tt-drawer{position:fixed;top:0;left:0;bottom:0;width:min(320px,86vw);background:#fff;z-index:91;transform:translateX(-102%);transition:transform .22s ease;display:flex;flex-direction:column;box-shadow:4px 0 24px rgba(11,30,63,.12);font-family:'Inter',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
  .tt-open .tt-shade{opacity:1;pointer-events:auto}
  .tt-open .tt-drawer{transform:none}
  .tt-me{display:flex;align-items:center;gap:12px;padding:20px 18px 16px;border-bottom:1px solid #DDE3EE;text-decoration:none;color:#0B1E3F}
  .tt-ava{width:52px;height:52px;border-radius:50%;background:#EAF0FA;overflow:hidden;display:flex;align-items:center;justify-content:center;font-weight:600;font-size:20px;flex:none}
  .tt-ava img{width:100%;height:100%;object-fit:cover}
  .tt-me b{display:block;font-size:16px;font-weight:600}
  .tt-me span{font-size:13px;color:#5A6B8C}
  .tt-list{overflow-y:auto;flex:1;padding:8px 8px 16px}
  .tt-item{display:flex;align-items:center;gap:12px;padding:11px 12px;border-radius:10px;color:#0B1E3F;text-decoration:none;font-size:15px}
  .tt-item:hover{background:#F3F6FB}
  .tt-item.on{background:#EAF0FA;font-weight:600}
  .tt-item svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
  .tt-item .tt-n{margin-left:auto;min-width:22px;height:22px;padding:0 7px;border-radius:11px;background:#B42318;color:#fff;font-size:12px;font-weight:700;display:none;align-items:center;justify-content:center}
  .tt-item .tt-c{margin-left:auto;font-size:13px;color:#5A6B8C;font-weight:600}
  .tt-sec{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#8A97AD;padding:16px 12px 6px}
  .tt-goal{padding:9px 12px;font-size:14px}
  .tt-goal i{width:9px;height:9px;border-radius:50%;flex:none;margin:0 5px}
  @media (max-width:760px){
    input,select,textarea{font-size:16px!important}
    header{padding:10px 12px!important}
    header a.dash,header a.back{display:none!important}
  }`;
  document.head.appendChild(css);

  const I = {
    match: '<path d="M8 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm8 0a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM2 19c0-3 3-5 6-5s6 2 6 5M12 15.5c1-.9 2.4-1.5 4-1.5 3 0 6 2 6 5"/>',
    notif: '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4zM10 20a2 2 0 0 0 4 0"/>',
    msg: '<path d="M4 5h16v11H8l-4 4z"/>',
    badge: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 7h6M12 11a2 2 0 1 0 0 4 2 2 0 0 0 0-4z"/>',
    out: '<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10"/>',
  };
  const GOAL_COLOR = { hire: "#0B1E3F", job: "#2E63B0", mentor: "#2E7D5B", find_mentor: "#6FA287", cofounder: "#B26B00", exchange: "#8A97AD" };
  const on = p => (p === "/app" && path === "/app") || (p !== "/app" && path.startsWith(p)) ? " on" : "";
  const item = (href, icon, label, extra) =>
    `<a class="tt-item${on(href)}" href="${href}"><svg viewBox="0 0 24 24">${I[icon]}</svg><span>${label}</span>${extra || ""}</a>`;

  const root = document.createElement("div");
  root.innerHTML = `<div class="tt-shade"></div>
  <aside class="tt-drawer" aria-label="Меню">
    <a class="tt-me" href="/profile"><div class="tt-ava" id="tt-ava">?</div><div><b id="tt-name">Профиль</b><span>Мой профиль</span></div></a>
    <div class="tt-list">
      ${item("/app", "match", "Подбор знакомств")}
      ${item("/notifications", "notif", "Уведомления", '<b class="tt-n" id="tt-notif"></b>')}
      ${item("/messages", "msg", "Сообщения", '<b class="tt-n" id="tt-msg"></b>')}
      <div class="tt-sec">Открыты к общению</div>
      <div id="tt-goals"></div>
      <div class="tt-sec">Ещё</div>
      ${item("/setup", "badge", "Мой NFC-бейдж")}
      <a class="tt-item" href="#" id="tt-logout"><svg viewBox="0 0 24 24">${I.out}</svg><span>Выйти</span></a>
    </div>
  </aside>`;
  document.body.appendChild(root);

  const btn = document.createElement("button");
  btn.className = "tt-dots";
  btn.setAttribute("aria-label", "Меню");
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg><b class="tt-sum" id="tt-sum"></b>';
  let header = document.querySelector("header");
  if (!header) {
    header = document.createElement("header");
    header.className = "tt-bar";
    header.innerHTML = '<a href="/app"><svg viewBox="0 0 120 120" width="28" height="28"><rect x="24" y="56" width="36" height="48" rx="16" fill="#0B1E3F"/><rect x="60" y="56" width="36" height="48" rx="16" fill="#0B1E3F"/><circle cx="42" cy="36" r="16" fill="#2E63B0"/><circle cx="78" cy="36" r="16" fill="#2E63B0"/><path d="M32 66 Q60 102 88 76" fill="none" stroke="#5B86C6" stroke-width="12" stroke-linecap="round"/><path d="M88 66 Q60 94 32 80" fill="none" stroke="#8FB0E0" stroke-width="12" stroke-linecap="round"/></svg>TanysTap</a>';
    document.body.prepend(header);
  }
  const left = document.createElement("div");
  left.className = "tt-left";
  const first = header.firstElementChild;
  header.insertBefore(left, first);
  left.append(btn);
  if (first) left.append(first);

  const open = v => document.body.classList.toggle("tt-open", v);
  btn.onclick = () => { open(true); refresh(); };
  root.querySelector(".tt-shade").onclick = () => open(false);
  document.addEventListener("keydown", e => { if (e.key === "Escape") open(false); });
  document.getElementById("tt-logout").onclick = e => {
    e.preventDefault();
    try { localStorage.removeItem("sid"); } catch (x) {}
    location.href = "/";
  };

  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  function setN(id, n) {
    const d = document.getElementById(id);
    if (!d) return;
    d.textContent = n > 9 ? "9+" : String(n);
    d.style.display = n > 0 ? "inline-flex" : "none";
  }

  async function checkAccount() {
    try {
      const r = await fetch(`/api/me?session_id=${encodeURIComponent(sid)}`).then(r => r.json());
      if (!r.account) { location.replace("/join?next=" + encodeURIComponent(here)); return; }
      if (!r.exists && path !== "/app") { location.replace("/join?step=2&next=" + encodeURIComponent(here)); return; }
      const p = r.profile || {};
      document.getElementById("tt-name").textContent = p.name || r.login || "Профиль";
      const ava = document.getElementById("tt-ava");
      if (p.avatar) { ava.innerHTML = ""; const img = new Image(); img.src = p.avatar; img.alt = ""; ava.appendChild(img); }
      else ava.textContent = (p.name || r.login || "?").slice(0, 1).toUpperCase();
    } catch (e) {}
  }

  async function refresh() {
    try {
      const c = await fetch(`/api/counts?session_id=${encodeURIComponent(sid)}`).then(r => r.json());
      const notif = path.startsWith("/notifications") ? 0 : c.notifications;
      setN("tt-msg", c.messages);
      setN("tt-notif", notif);
      setN("tt-sum", c.messages + notif);
    } catch (e) {}
  }
  async function goals() {
    try {
      const g = await fetch(`/api/people?session_id=${encodeURIComponent(sid)}`).then(r => r.json());
      const order = ["hire", "job", "mentor", "find_mentor", "cofounder", "exchange"];
      document.getElementById("tt-goals").innerHTML = order.map(k =>
        `<a class="tt-item tt-goal${location.search.includes("goal=" + k) ? " on" : ""}" href="/people?goal=${k}"><i style="background:${GOAL_COLOR[k]}"></i><span>${esc(g.labels[k])}</span><span class="tt-c">${g.counts[k] || 0}</span></a>`).join("");
    } catch (e) {}
  }

  checkAccount();
  refresh();
  goals();
  setInterval(refresh, 8000);
  setInterval(goals, 30000);
  window.ttNavRefresh = refresh;
  window.ttNavSetLang = () => {};
})();
