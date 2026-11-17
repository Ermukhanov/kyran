/* Kyran Mini App — без сборки и фреймворков. Работает в Telegram и в браузере (ссылка /web). */
(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const view = document.getElementById("view");
  const STAGES = [
    ["saved", "Интересно"], ["preparing", "Готовим"], ["submitted", "Подали"], ["won", "Выиграли"], ["lost", "Проиграли"],
  ];
  const STAGE_NAME = Object.fromEntries(STAGES);
  const AMOUNTS = [
    ["Любые", null, null], ["До 5 млн", null, 5e6], ["5–50 млн", 5e6, 5e7], ["50–300 млн", 5e7, 3e8], ["От 300 млн", 3e8, null],
  ];
  const state = { me: null, filter: "all", query: "" };

  // ── Тема и Telegram ──────────────────────────────
  if (tg) {
    tg.ready();
    tg.expand();
    document.documentElement.dataset.theme = tg.colorScheme === "dark" ? "dark" : "light";
    tg.onEvent("themeChanged", () => { document.documentElement.dataset.theme = tg.colorScheme === "dark" ? "dark" : "light"; });
  } else if (window.matchMedia("(prefers-color-scheme: dark)").matches) {
    document.documentElement.dataset.theme = "dark";
  }
  const haptic = (t = "light") => { try { tg && tg.HapticFeedback.impactOccurred(t); } catch (_) {} };

  // ── Авторизация ─────────────────────────────────
  const params = new URLSearchParams(location.search);
  if (params.get("t")) {
    try { localStorage.setItem("kyran_token", params.get("t")); } catch (_) {}
    history.replaceState(null, "", location.pathname + location.hash);
  }
  function authHeader() {
    if (tg && tg.initData) return "tma " + tg.initData;
    let t = null;
    try { t = localStorage.getItem("kyran_token"); } catch (_) {}
    return t ? "Bearer " + t : "";
  }

  async function api(path, opts = {}) {
    const res = await fetch("/api" + path, {
      ...opts,
      headers: { "Content-Type": "application/json", Authorization: authHeader(), ...(opts.headers || {}) },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!res.ok) {
      let msg = "Ошибка " + res.status;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      const err = new Error(msg); err.status = res.status; throw err;
    }
    return res.json();
  }

  // ── Утилиты ─────────────────────────────────────
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function money(n, short = true) {
    if (n == null) return "сумма не указана";
    if (short && n >= 1e9) return (n / 1e9).toFixed(1).replace(".0", "").replace(".", ",") + " млрд ₸";
    if (short && n >= 1e6) return (n / 1e6).toFixed(1).replace(".0", "").replace(".", ",") + " млн ₸";
    return Math.round(n).toLocaleString("ru-RU") + " ₸";
  }
  function plural(n, one, few, many) {
    const m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }
  function leftLabel(hours) {
    if (hours == null) return "срок не указан";
    if (hours < 0) return "приём закрыт";
    if (hours < 24) return Math.max(1, Math.floor(hours)) + " ч";
    const d = Math.floor(hours / 24);
    return d + " " + plural(d, "день", "дня", "дней");
  }
  function deadlineDate(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Almaty" });
  }
  function fuse(hours, big = false) {
    // Полоса = доля оставшегося времени из 14 дней. Догорает: бирюза → золото → красный.
    const left = hours == null ? 100 : Math.max(2, Math.min(100, (hours / (14 * 24)) * 100));
    const heat = hours == null ? "" : hours < 72 ? "hot" : hours < 24 * 7 ? "warm" : "";
    return `<div class="fuse ${heat} ${big ? "big" : ""}" title="Осталось на подачу заявки">
      <div class="fuse-track"><div class="fuse-burn" style="--left:${left}"></div></div>
      <span class="fuse-label">${esc(leftLabel(hours))}</span></div>`;
  }
  function scoreRing(score) {
    if (score == null) return `<div class="score none" aria-label="Без оценки"><span>—</span></div>`;
    return `<div class="score" style="--p:${score}" aria-label="Совпадение ${score} процентов"><span>${score}</span></div>`;
  }
  function toast(text) {
    const el = document.getElementById("toast");
    el.textContent = text; el.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => (el.hidden = true), 2600);
  }
  const skeleton = (n = 4) => `<div aria-hidden="true">${'<div class="skeleton"></div>'.repeat(n)}</div>`;

  function lotRow(l) {
    const meta = [
      `<span class="lot-amount">${esc(money(l.amount))}</span>`,
      l.customer ? `<span>${esc(l.customer)}</span>` : "",
      l.stage ? `<span class="stage-tag">${esc(STAGE_NAME[l.stage] || l.stage)}</span>` : "",
      l.is_demo ? `<span class="demo-tag">демо</span>` : "",
    ].join("");
    return `<li><a class="lot" href="#lot/${l.id}">
      ${scoreRing(l.score)}
      <div><p class="lot-title">${esc(l.title)}</p><div class="lot-meta">${meta}</div>${fuse(l.hours_left)}</div>
    </a></li>`;
  }

  // ── Экраны ──────────────────────────────────────
  async function loadMe() {
    state.me = await api("/me");
    document.getElementById("ai-badge").hidden = !state.me.features.ai;
    document.getElementById("demo-strip").hidden = !state.me.features.demo;
    return state.me;
  }

  function noAccess(message) {
    view.innerHTML = `<div class="empty welcome">
      <h1>Откройте Kyran через Telegram</h1>
      <p>${esc(message || "Найдите бота Kyran в Telegram и нажмите «Открыть Kyran». Для компьютера отправьте боту команду /web — придёт личная ссылка.")}</p>
    </div>`;
  }

  async function renderFeed() {
    const me = state.me;
    if (!me.user.onboarded) return renderOnboarding();
    const s = me.stats;
    const name = (me.user.name || "").split(" ")[0];
    view.innerHTML = `
      <section class="horizon">
        ${name ? `<div class="greeting">${esc(greet())}, ${esc(name)}</div>` : ""}
        <div class="horizon-count">${s.open}</div>
        <div class="horizon-text">${plural(s.open, "открытый тендер", "открытых тендера", "открытых тендеров")} под вас
          на <strong>${esc(money(s.total_amount))}</strong>${s.urgent ? `, <span class="horizon-hot">${s.urgent} ${plural(s.urgent, "горит", "горят", "горят")}</span>` : ""}</div>
      </section>
      <input class="search" type="search" placeholder="Поиск по названию или заказчику" value="${esc(state.query)}" aria-label="Поиск">
      <div class="filters" role="group" aria-label="Фильтр">
        <button class="chip" data-f="all" aria-pressed="${state.filter === "all"}">Все</button>
        <button class="chip" data-f="urgent" aria-pressed="${state.filter === "urgent"}">Горят</button>
        <button class="chip" data-f="anywhere">Искать везде</button>
      </div>
      <ul class="lots" id="lots">${skeleton()}</ul>`;

    const input = view.querySelector(".search");
    let timer;
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { state.query = input.value.trim(); loadLots(); }, 300); });
    view.querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.f === "anywhere") return searchEverywhere(input.value.trim());
      state.filter = b.dataset.f;
      view.querySelectorAll(".chip").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      loadLots();
    }));
    loadLots();
  }

  function greet() {
    const h = new Date().getHours();
    return h < 5 ? "Доброй ночи" : h < 12 ? "Доброе утро" : h < 18 ? "Добрый день" : "Добрый вечер";
  }

  async function loadLots() {
    const box = document.getElementById("lots");
    if (!box) return;
    try {
      const q = new URLSearchParams({ q: state.query, urgent: state.filter === "urgent" });
      const { items } = await api("/feed?" + q);
      const sources = state.me.features.sources;
      const noData = !Object.values(sources).some(Boolean);
      if (!items.length) {
        box.innerHTML = `<li class="empty">${noData
          ? "<h2>Подключаем базу госзакупок</h2>Как только данные пойдут, подходящие тендеры появятся здесь и в боте. Делать ничего не нужно."
          : state.query
            ? `<h2>Среди ваших тендеров такого нет</h2>Нажмите «Искать везде» — поищем по всей базе.`
            : "<h2>Пока подходящих тендеров нет</h2>Добавьте направления в профиле — чем точнее, тем больше находок."}</li>`;
        return;
      }
      box.innerHTML = items.map(lotRow).join("");
    } catch (e) { box.innerHTML = `<li class="empty">${esc(e.message)}</li>`; }
  }

  async function searchEverywhere(q) {
    if (q.length < 3) return toast("Напишите хотя бы 3 буквы");
    const box = document.getElementById("lots");
    box.innerHTML = skeleton(3);
    const { items } = await api("/search?q=" + encodeURIComponent(q));
    box.innerHTML = items.length ? items.map(lotRow).join("") : `<li class="empty"><h2>Ничего не нашли</h2>Попробуйте другие слова.</li>`;
  }

  async function renderLot(id) {
    view.innerHTML = skeleton(5);
    if (tg) { tg.BackButton.show(); tg.BackButton.onClick(goBack); }
    let data;
    try { data = await api("/lots/" + id); } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    const l = data.lot;
    const docs = (l.documents || []).filter((d) => d.url);
    view.innerHTML = `
      ${tg ? "" : `<button class="btn btn-quiet back" data-act="back">‹ Назад</button>`}
      ${l.is_demo ? `<span class="demo-tag">демо-пример</span>` : ""}
      <h1 class="detail-title">${esc(l.title)}</h1>
      <p class="detail-customer">${esc(l.customer || "Заказчик не указан")}${l.region ? ", " + esc(l.region) : ""}</p>
      <div class="figures">
        <div><div class="figure-value">${esc(money(l.amount, false))}</div><div class="figure-label">сумма закупки</div></div>
        <div><div class="figure-value">${esc(deadlineDate(l.deadline) || "—")}</div><div class="figure-label">приём заявок до</div></div>
        <div><div class="figure-value">${l.score != null ? l.score + "%" : "—"}</div><div class="figure-label">совпадение с вами</div></div>
      </div>
      ${fuse(l.hours_left, true)}
      ${l.reason ? `<p class="reason">${esc(l.reason)}</p>` : ""}
      ${l.description ? `<h2 class="section-title">Что закупают</h2><p>${esc(l.description)}</p>` : ""}
      ${l.method ? `<p class="detail-customer">Способ закупки: ${esc(l.method)}</p>` : ""}

      <h2 class="section-title">Ваш статус</h2>
      <div class="stages" role="group" aria-label="Стадия в воронке">
        ${STAGES.map(([k, n]) => `<button class="chip" data-stage="${k}" aria-pressed="${l.stage === k}">${n}</button>`).join("")}
      </div>
      <div class="field"><textarea class="note-area" placeholder="Заметка для себя: цена, контакты, что подготовить">${esc(data.note)}</textarea></div>

      <h2 class="section-title">ИИ-разбор</h2>
      <div id="analysis"></div>

      ${docs.length ? `<h2 class="section-title">Документация</h2><ul class="alist">${docs.map((d) => `<li><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.name)}</a></li>`).join("")}</ul>` : ""}
      <div class="btn-row">
        ${l.url ? `<a class="btn" href="${esc(l.url)}" target="_blank" rel="noopener">Открыть на портале</a>` : ""}
        <button class="btn" data-act="hide">Не интересно</button>
      </div>`;

    renderAnalysis(id, data.analysis, data.ai);
    const back = view.querySelector('[data-act="back"]');
    if (back) back.addEventListener("click", goBack);
    view.querySelectorAll("[data-stage]").forEach((b) => b.addEventListener("click", async () => {
      const next = b.getAttribute("aria-pressed") === "true" ? null : b.dataset.stage;
      await api(`/lots/${id}/stage`, { method: "POST", body: { stage: next } });
      view.querySelectorAll("[data-stage]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.stage === next)));
      haptic(); toast(next ? `Перенесено в «${STAGE_NAME[next]}»` : "Убрано из воронки");
    }));
    const note = view.querySelector(".note-area");
    note.addEventListener("change", async () => {
      const current = view.querySelector('[data-stage][aria-pressed="true"]');
      await api(`/lots/${id}/stage`, { method: "POST", body: { stage: current ? current.dataset.stage : "saved", note: note.value } });
      if (!current) view.querySelector('[data-stage="saved"]').setAttribute("aria-pressed", "true");
      toast("Заметка сохранена");
    });
    view.querySelector('[data-act="hide"]').addEventListener("click", async () => {
      await api(`/lots/${id}/hide`, { method: "POST" });
      toast("Скрыто из ленты"); goBack();
    });
  }

  const VERDICT = {
    go: ["✓", "Стоит участвовать"],
    consider: ["?", "Стоит подумать"],
    skip: ["✕", "Лучше пропустить"],
  };

  function renderAnalysis(id, a, aiOn) {
    const box = document.getElementById("analysis");
    if (!a) {
      box.innerHTML = aiOn
        ? `<p class="detail-customer">ИИ прочитает закупку и документацию и скажет: участвовать или нет, какие требования, документы и риски. Обычно 30–60 секунд.</p>
           <button class="btn btn-primary" data-act="analyze">Разобрать тендер</button>`
        : `<p class="detail-customer">ИИ-разбор появится, когда к Kyran подключат ИИ. Остальное уже работает.</p>`;
      const btn = box.querySelector('[data-act="analyze"]');
      if (btn) btn.addEventListener("click", () => runAnalysis(id, false));
      return;
    }
    const [mark, title] = VERDICT[a.verdict] || VERDICT.consider;
    const key = "kyran_docs_" + id;
    let checked = {};
    try { checked = JSON.parse(localStorage.getItem(key) || "{}"); } catch (_) {}
    const list = (arr, fn) => (arr && arr.length ? `<ul class="alist">${arr.map(fn).join("")}</ul>` : `<p class="detail-customer">Нет данных</p>`);
    box.innerHTML = `
      <div class="verdict ${esc(a.verdict)}"><div class="verdict-mark" aria-hidden="true">${mark}</div>
        <div><div class="verdict-title">${title}${a.score != null ? ` · ${esc(a.score)}%` : ""}</div><div class="verdict-text">${esc(a.summary)}</div></div></div>
      ${a.scope && a.scope.length ? `<h3 class="section-title">Объём</h3>${list(a.scope, (x) => `<li>${esc(x)}</li>`)}` : ""}
      <h3 class="section-title">Требования к вам</h3>
      ${list(a.requirements, (r) => `<li>${esc(r.text || r)}${r.source ? `<span class="src">${esc(r.source)}</span>` : ""}</li>`)}
      <h3 class="section-title">Документы для заявки</h3>
      ${list(a.documents, (d, i) => `<li><label class="check"><input type="checkbox" data-doc="${i}" ${checked[i] ? "checked" : ""}><span>${esc(d)}</span></label></li>`)}
      <h3 class="section-title">Риски</h3>
      ${list(a.risks, (r) => `<li class="risk ${esc(r.level || "")}">${esc(r.text || r)}</li>`)}
      ${a.questions && a.questions.length ? `<h3 class="section-title">Спросить у заказчика</h3>${list(a.questions, (q) => `<li>${esc(q)}</li>`)}` : ""}
      ${a.next_steps && a.next_steps.length ? `<h3 class="section-title">Что сделать сейчас</h3>${list(a.next_steps, (q) => `<li>${esc(q)}</li>`)}` : ""}
      <p class="detail-customer" style="margin-top:14px">${a.docs_used && a.docs_used.length ? "Разобраны документы: " + esc(a.docs_used.join(", ")) + "." : "Разбор по карточке закупки — документация не загружалась."} ИИ может ошибаться, проверяйте важное по первоисточнику.</p>
      <button class="btn btn-quiet" data-act="reanalyze">Разобрать заново</button>`;
    box.querySelectorAll("[data-doc]").forEach((c) => c.addEventListener("change", () => {
      checked[c.dataset.doc] = c.checked;
      try { localStorage.setItem(key, JSON.stringify(checked)); } catch (_) {}
    }));
    box.querySelector('[data-act="reanalyze"]').addEventListener("click", () => runAnalysis(id, true));
  }

  async function runAnalysis(id, refresh) {
    const box = document.getElementById("analysis");
    box.innerHTML = `<div class="thinking"><svg class="thinking-bird" viewBox="0 0 32 32" aria-hidden="true"><path d="M1 10.5c5.5-.4 9.6 1.8 13 5.6l.6-2.3c.3-1 .8-1.5 1.4-1.5s1.1.5 1.4 1.5l.6 2.3c3.4-3.8 7.5-6 13-5.6-4.6 1.6-8.2 4.6-10.6 8.8L18 23.5l-2 3-2-3-1.4-4.2C10.2 15.1 5.6 12.1 1 10.5z" fill="currentColor"/></svg>Читаю закупку и документацию…</div>`;
    try {
      const { analysis } = await api(`/lots/${id}/analyze?refresh=${refresh}`, { method: "POST" });
      haptic("medium");
      renderAnalysis(id, analysis, true);
    } catch (e) {
      box.innerHTML = `<p class="detail-customer">${esc(e.message)}</p><button class="btn" data-act="retry">Попробовать снова</button>`;
      box.querySelector('[data-act="retry"]').addEventListener("click", () => runAnalysis(id, refresh));
    }
  }

  async function renderPipeline() {
    view.classList.add("wide");
    view.innerHTML = `<h1 class="section-title" style="font-size:22px;margin-top:10px">Воронка</h1>${skeleton(3)}`;
    const { columns } = await api("/pipeline");
    const total = Object.values(columns).reduce((a, c) => a + c.length, 0);
    const cols = STAGES.map(([k, n]) => {
      const items = columns[k] || [];
      const sum = items.reduce((a, x) => a + (x.amount || 0), 0);
      return `<section class="col ${k}">
        <div class="col-head"><h3>${n}</h3><span>${items.length}</span></div>
        ${items.length ? items.map((x) => `<a class="mini" href="#lot/${x.id}"><div class="mini-title">${esc(x.title)}</div>
          <div class="mini-meta">${esc(money(x.amount))}, ${esc(leftLabel(x.hours_left))}${x.note ? " · заметка" : ""}</div></a>`).join("")
          : `<div class="col-empty">Пусто</div>`}
        ${sum ? `<div class="mini-meta" style="padding-top:8px">Всего ${esc(money(sum))}</div>` : ""}
      </section>`;
    }).join("");
    view.innerHTML = `<h1 class="section-title" style="font-size:22px;margin-top:10px">Воронка</h1>
      ${total ? "" : `<p class="detail-customer">Отмечайте тендеры кнопкой «Интересно» — они соберутся здесь, и вы будете вести их до победы.</p>`}
      <div class="board">${cols}</div>`;
  }

  async function renderSites() {
    view.innerHTML = skeleton(3);
    const { items, ai } = await api("/monitors");
    view.innerHTML = `
      <div class="welcome"><h1>Слежу за любым сайтом</h1>
        <p>Вставьте ссылку на страницу с закупками — сайт компании, отраслевую площадку, раздел «Тендеры». ${ai
          ? "Когда появится новое, ИИ вытащит закупки и пришлёт их в бот."
          : "Когда страница изменится, пришлю уведомление в бот."}</p></div>
      <form class="field" id="site-form" style="display:flex;gap:8px">
        <input class="input" name="url" type="url" inputmode="url" placeholder="https://..." required aria-label="Ссылка на сайт">
        <button class="btn btn-primary" type="submit">Следить</button>
      </form>
      <div id="sites">${items.length ? items.map((m) => `
        <div class="site"><div><div class="site-title">${esc(m.title)}</div><div class="site-url">${esc(m.url)}</div>
          <div class="site-status ${m.error ? "err" : ""}">${m.error ? "Не открывается: " + esc(m.error)
            : m.last_change ? "Последнее изменение: " + esc(deadlineDate(m.last_change)) : "Изменений пока не было"}</div></div>
          <button class="btn btn-quiet" data-del="${m.id}" aria-label="Перестать следить за ${esc(m.title)}">Убрать</button></div>`).join("")
        : `<p class="empty">Пока ни одного сайта.</p>`}</div>`;
    view.querySelector("#site-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = e.target.querySelector("button");
      btn.disabled = true; btn.textContent = "Проверяю…";
      try { await api("/monitors", { method: "POST", body: { url: e.target.url.value } }); toast("Слежу за сайтом"); renderSites(); }
      catch (err) { toast(err.message); btn.disabled = false; btn.textContent = "Следить"; }
    });
    view.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      await api("/monitors/" + b.dataset.del, { method: "DELETE" }); toast("Больше не слежу"); renderSites();
    }));
  }

  function keywordEditor(initial) {
    const kws = [...initial];
    const wrap = document.createElement("div");
    wrap.className = "kw-box";
    const draw = () => {
      wrap.innerHTML = kws.map((k, i) => `<span class="kw">${esc(k)}<button type="button" data-i="${i}" aria-label="Убрать ${esc(k)}">×</button></span>`).join("")
        + `<input class="kw-input" placeholder="${kws.length ? "Ещё направление и Enter" : "например: ремонт кровли"}" aria-label="Добавить направление">`;
      wrap.querySelectorAll("[data-i]").forEach((b) => b.addEventListener("click", () => { kws.splice(+b.dataset.i, 1); draw(); }));
      const inp = wrap.querySelector("input");
      inp.addEventListener("keydown", (e) => {
        if ((e.key === "Enter" || e.key === ",") && inp.value.trim()) {
          e.preventDefault();
          inp.value.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean).forEach((x) => { if (!kws.includes(x)) kws.push(x); });
          draw(); wrap.querySelector("input").focus();
        }
      });
    };
    draw();
    wrap.value = () => {
      const pending = wrap.querySelector("input").value.trim().toLowerCase();
      return pending && !kws.includes(pending) ? [...kws, pending] : kws;
    };
    return wrap;
  }

  function amountPicker(min, max) {
    const row = document.createElement("div");
    row.className = "radio-row"; row.setAttribute("role", "group"); row.setAttribute("aria-label", "Суммы");
    let sel = AMOUNTS.findIndex(([, a, b]) => a === min && b === max);
    if (sel < 0) sel = 0;
    const draw = () => {
      row.innerHTML = AMOUNTS.map(([n], i) => `<button type="button" class="chip" data-i="${i}" aria-pressed="${i === sel}">${n}</button>`).join("");
      row.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => { sel = +b.dataset.i; draw(); }));
    };
    draw();
    row.value = () => ({ min_amount: AMOUNTS[sel][1], max_amount: AMOUNTS[sel][2] });
    return row;
  }

  function renderOnboarding() {
    view.innerHTML = `<div class="welcome">
      <h1>Найдём тендеры под вашу компанию</h1>
      <p>Укажите, что вы поставляете или какие работы выполняете. Kyran будет присылать только подходящие закупки.</p></div>
      <div class="field"><label>Направления</label><div class="hint">Через запятую или Enter</div><div id="kw"></div></div>
      <div class="field"><label>Суммы закупок</label><div id="am"></div></div>
      <button class="btn btn-primary" id="go" style="width:100%">Показать тендеры</button>`;
    const kw = keywordEditor([]); view.querySelector("#kw").append(kw);
    const am = amountPicker(null, null); view.querySelector("#am").append(am);
    view.querySelector("#go").addEventListener("click", async (e) => {
      const keywords = kw.value();
      if (!keywords.length) return toast("Добавьте хотя бы одно направление");
      e.target.disabled = true; e.target.textContent = "Подбираю…";
      await api("/me", { method: "PUT", body: { keywords, ...am.value() } });
      await loadMe(); haptic("medium"); location.hash = "#feed"; route();
    });
  }

  function renderProfile() {
    const u = state.me.user;
    view.innerHTML = `
      <h1 class="section-title" style="font-size:22px;margin-top:10px">Профиль компании</h1>
      <div class="field"><label>Направления</label><div class="hint">По ним Kyran подбирает тендеры</div><div id="kw"></div></div>
      <div class="field"><label>Суммы закупок</label><div id="am"></div></div>
      <div class="field"><label for="about">О компании</label><div class="hint">Опыт, лицензии, техника, где работаете — ИИ учтёт при оценке</div>
        <textarea id="about" class="note-area" maxlength="2000">${esc(u.about)}</textarea></div>
      <div class="field"><label for="bin">БИН</label><input id="bin" class="input" inputmode="numeric" maxlength="12" value="${esc(u.company_bin)}" placeholder="12 цифр"></div>
      <label class="switch"><span>Уведомления в Telegram</span><input type="checkbox" id="notify" ${u.notify ? "checked" : ""}></label>
      <button class="btn btn-primary" id="save" style="width:100%;margin-top:20px">Сохранить</button>
      <p class="detail-customer" style="margin-top:20px">На компьютере: отправьте боту команду /web — придёт личная ссылка на эту же страницу.</p>`;
    const kw = keywordEditor(u.keywords); view.querySelector("#kw").append(kw);
    const am = amountPicker(u.min_amount, u.max_amount); view.querySelector("#am").append(am);
    view.querySelector("#save").addEventListener("click", async (e) => {
      const bin = view.querySelector("#bin").value.replace(/\D/g, "");
      if (bin && bin.length !== 12) return toast("БИН — это 12 цифр");
      e.target.disabled = true;
      try {
        await api("/me", { method: "PUT", body: {
          keywords: kw.value(), ...am.value(), about: view.querySelector("#about").value,
          company_bin: bin, notify: view.querySelector("#notify").checked,
        } });
        await loadMe(); haptic(); toast("Сохранено, подборка обновлена");
      } catch (err) { toast(err.message); }
      e.target.disabled = false;
    });
  }

  // ── Роутер ──────────────────────────────────────
  function goBack() {
    if (history.length > 1) history.back(); else location.hash = "#feed";
  }

  async function route() {
    const [page, arg] = (location.hash.slice(1) || "feed").split("/");
    view.classList.remove("wide");
    if (tg && page !== "lot") { tg.BackButton.hide(); tg.BackButton.offClick(goBack); }
    document.querySelectorAll(".tabs a").forEach((a) => {
      if (a.dataset.tab === page) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    window.scrollTo(0, 0);
    try {
      if (!state.me) await loadMe();
      if (page === "lot" && arg) return await renderLot(arg);
      if (page === "pipeline") return await renderPipeline();
      if (page === "sites") return await renderSites();
      if (page === "profile") return renderProfile();
      return await renderFeed();
    } catch (e) {
      if (e.status === 401) return noAccess(e.message.replace(/^Нет доступа: /, ""));
      view.innerHTML = `<div class="empty"><h2>Не загрузилось</h2>${esc(e.message)}<div class="btn-row"><button class="btn" onclick="location.reload()">Обновить</button></div></div>`;
    }
  }

  window.addEventListener("hashchange", route);
  if (!authHeader()) noAccess(); else route();
})();
