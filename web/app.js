/* Kyran — веб-платформа и Telegram Mini App. Без сборки и фреймворков. */
(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData ? window.Telegram.WebApp : null;
  const view = document.getElementById("view");
  const STAGES = [
    ["saved", "Интересно"], ["preparing", "Подготовка"], ["submitted", "Заявка подана"], ["won", "Победа"], ["lost", "Не выиграли"],
  ];
  const STAGE_NAME = Object.fromEntries(STAGES);
  const AMOUNTS = [
    ["Любые", null, null], ["до 5 млн ₸", null, 5e6], ["5–50 млн ₸", 5e6, 5e7], ["50–300 млн ₸", 5e7, 3e8], ["от 300 млн ₸", 3e8, null],
  ];
  const state = { me: null, filter: "all", query: "", sort: { key: "score", dir: -1 }, items: [] };

  // ── Тема ────────────────────────────────────────
  // Светлая тема всегда — так интерфейс выглядит одинаково в Telegram и в браузере.
  function applyTheme() { document.documentElement.dataset.theme = "light"; }
  if (tg) { tg.ready(); tg.expand(); try { tg.setHeaderColor("#FFFFFF"); tg.setBackgroundColor("#F5F6F8"); } catch (_) {} }
  applyTheme();

  // ── Авторизация ─────────────────────────────────
  const params = new URLSearchParams(location.search);
  if (params.get("t")) {
    try { localStorage.setItem("kyran_token", params.get("t")); } catch (_) {}
    history.replaceState(null, "", location.pathname + location.hash);
  }
  function authHeader() {
    if (tg) return "tma " + tg.initData;
    let t = null;
    try { t = localStorage.getItem("kyran_token"); } catch (_) {}
    return t ? "Bearer " + t : "";
  }
  async function api(path, opts = {}) {
    const isForm = opts.body instanceof FormData;
    const headers = { Authorization: authHeader() };
    if (!isForm) headers["Content-Type"] = "application/json";
    const res = await fetch("/api" + path, {
      method: opts.method || "GET", headers,
      body: opts.body ? (isForm ? opts.body : JSON.stringify(opts.body)) : undefined,
    });
    if (!res.ok) {
      let msg = "Ошибка " + res.status;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      const err = new Error(msg); err.status = res.status; throw err;
    }
    return res.json();
  }
  function setToken(t) { try { t ? localStorage.setItem("kyran_token", t) : localStorage.removeItem("kyran_token"); } catch (_) {} }

  // ── Форматирование ──────────────────────────────
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const TZ = "Asia/Almaty";
  function money(n) {
    if (n == null) return "—";
    return Math.round(n).toLocaleString("ru-RU") + " ₸";
  }
  function moneyShort(n) {
    if (n == null) return "—";
    if (n >= 1e9) return (n / 1e9).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + " млрд ₸";
    if (n >= 1e6) return (n / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + " млн ₸";
    return money(n);
  }
  function plural(n, one, few, many) {
    const a = n % 10, b = n % 100;
    if (a === 1 && b !== 11) return one;
    if (a >= 2 && a <= 4 && (b < 12 || b > 14)) return few;
    return many;
  }
  function dateShort(iso) {
    if (!iso) return "—";
    return new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: TZ });
  }
  function dateTime(iso) {
    if (!iso) return "—";
    return new Date(iso).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit", timeZone: TZ });
  }
  function timeLeft(h) {
    if (h == null) return { text: "срок не указан", cls: "" };
    if (h < 0) return { text: "приём завершён", cls: "" };
    if (h < 24) return { text: `осталось ${Math.max(1, Math.floor(h))} ч`, cls: "bad" };
    const d = Math.floor(h / 24);
    return { text: `осталось ${d} ${plural(d, "день", "дня", "дней")}`, cls: h < 72 ? "bad" : h < 24 * 7 ? "warn" : "" };
  }
  function toast(text) {
    const el = document.getElementById("toast");
    el.textContent = text; el.hidden = false;
    clearTimeout(toast.t); toast.t = setTimeout(() => (el.hidden = true), 2600);
  }
  const haptic = () => { try { tg && tg.HapticFeedback.impactOccurred("light"); } catch (_) {} };
  const matchCell = (score) => score == null ? `<span class="cell-sub">—</span>`
    : `<div class="match" title="Совпадение с профилем компании"><span class="match-num">${score}%</span><span class="match-bar"><i style="--p:${score}"></i></span></div>`;
  const stageTag = (st) => st ? `<span class="tag stage ${st === "won" ? "won" : st === "lost" ? "lost" : ""}">${esc(STAGE_NAME[st] || st)}</span>` : "";

  function initials(u) {
    const name = (u.company_name || u.name || "Kyran").replace(/[«»"'“”]/g, " ").replace(/^\s*(ТОО|ИП|АО|ТДО|LLP)\s+/i, "");
    return name.trim().split(/\s+/).filter((w) => /[\p{L}\d]/u.test(w[0])).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "K";
  }

  // ── Статус в боковой панели ─────────────────────
  function paintStatus() {
    const f = state.me.features;
    const real = Object.entries(f.sources).filter(([k, v]) => v && k !== "demo").length;
    document.getElementById("src-dot").className = "dot " + (real ? "on" : f.demo ? "demo" : "");
    document.getElementById("src-text").textContent = real ? "Портал госзакупок подключён" : f.demo ? "Демонстрационные данные" : "Источник не подключён";
    document.getElementById("ai-dot").className = "dot " + (f.ai ? "on" : "");
    document.getElementById("ai-text").textContent = f.ai ? "ИИ-модуль активен" : "ИИ-модуль не подключён";
    document.getElementById("demo-strip").hidden = !f.demo;
    const u = state.me.user, p = state.me.plan;
    const box = document.getElementById("user-box");
    box.innerHTML = `<span class="avatar">${esc(initials(u))}</span><span class="user-meta"><strong>${esc(u.company_name || u.name || "Моя компания")}</strong>
      <span class="plan-chip ${p.active ? "" : "off"}">${esc(p.name)}${p.until ? " · до " + esc(dateShort(p.until)) : ""}</span></span>`;
    box.hidden = false;
    const strip = document.getElementById("plan-strip");
    const daysLeft = p.until ? Math.ceil((new Date(p.until) - Date.now()) / 864e5) : null;
    if (!p.active) { strip.textContent = `${p.code === "trial" ? "Пробный период" : "Тариф"} закончился. Уведомления и ИИ-разборы приостановлены — выберите тариф.`; strip.hidden = false; }
    else if (p.code === "trial" && daysLeft != null && daysLeft <= 3) { strip.textContent = `Пробный период заканчивается через ${daysLeft} ${plural(daysLeft, "день", "дня", "дней")}. Выберите тариф, чтобы не потерять подборку.`; strip.hidden = false; }
    else strip.hidden = true;
  }
  async function loadMe() { state.me = await api("/me"); paintStatus(); return state.me; }

  // ── Тендеры ─────────────────────────────────────
  async function renderFeed() {
    if (!state.me.user.onboarded) return renderOnboarding();
    const s = state.me.stats;
    const inWork = Object.entries(s.pipeline || {}).filter(([k]) => k !== "lost" && k !== "won").reduce((a, [, v]) => a + v, 0);
    view.innerHTML = `
      <div class="page-head">
        <div><h1>Тендеры под вашу компанию</h1><p class="page-sub">Открытые закупки, отобранные по профилю. Отсортированы по совпадению.</p></div>
      </div>
      <a class="missed" id="missed" href="#report" hidden></a>
      <section class="kpis" aria-label="Сводка">
        <div class="kpi"><div class="kpi-label">Открыто под вас</div><div class="kpi-value">${s.open}</div></div>
        <div class="kpi"><div class="kpi-label">Общая сумма</div><div class="kpi-value">${esc(moneyShort(s.total_amount))}</div></div>
        <button class="kpi" data-act="urgent" title="Показать срочные"><div class="kpi-label">Срок подачи ≤ 3 дней</div><div class="kpi-value ${s.urgent ? "bad" : ""}">${s.urgent}</div></button>
        <a class="kpi" href="#pipeline" style="color:inherit"><div class="kpi-label">В работе</div><div class="kpi-value">${inWork}</div></a>
      </section>
      <div class="toolbar">
        <input class="search" type="search" placeholder="Поиск по названию или заказчику" value="${esc(state.query)}" aria-label="Поиск">
        <div class="segmented" role="group" aria-label="Фильтр">
          <button data-f="all" aria-pressed="${state.filter === "all"}">Все</button>
          <button data-f="urgent" aria-pressed="${state.filter === "urgent"}">Срочные</button>
        </div>
        <button class="btn" data-act="everywhere">Искать по всей базе</button>
      </div>
      <div class="table-wrap" id="results"><div>${'<div class="skeleton-row"></div>'.repeat(5)}</div></div>`;

    const input = view.querySelector(".search");
    let t;
    input.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { state.query = input.value.trim(); loadLots(); }, 300); });
    const setFilter = (f) => {
      state.filter = f;
      view.querySelectorAll("[data-f]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.f === f)));
      loadLots();
    };
    view.querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => setFilter(b.dataset.f)));
    view.querySelector('[data-act="urgent"]').addEventListener("click", () => setFilter("urgent"));
    view.querySelector('[data-act="everywhere"]').addEventListener("click", () => searchEverywhere(input.value.trim()));
    loadLots();
    api("/report/weekly").then((r) => {
      const el = document.getElementById("missed");
      if (!el || !r.missed.count) return;
      el.innerHTML = `<span class="missed-num">${esc(moneyShort(r.missed.total))}</span>
        <span class="missed-text">ушло конкурентам за неделю: ${r.missed.count} ${plural(r.missed.count, "подходящая закупка", "подходящие закупки", "подходящих закупок")}${r.missed.median_discount != null ? `, снижение в среднем ${r.missed.median_discount}%` : ""}</span>
        <span class="missed-link">Отчёт недели</span>`;
      el.hidden = false;
    }).catch(() => {});
  }

  async function loadLots() {
    try {
      const q = new URLSearchParams({ q: state.query, urgent: state.filter === "urgent" });
      state.items = (await api("/feed?" + q)).items;
      drawResults();
    } catch (e) { results(`<div class="empty"><p>${esc(e.message)}</p></div>`); }
  }

  function results(html) { const el = document.getElementById("results"); if (el) el.innerHTML = html; }

  const SORTERS = {
    score: (x) => x.score ?? -1,
    amount: (x) => x.amount ?? -1,
    deadline: (x) => (x.hours_left == null ? 1e9 : x.hours_left),
    title: (x) => x.title.toLowerCase(),
  };

  function drawResults(emptyText) {
    const items = [...state.items];
    const { key, dir } = state.sort;
    items.sort((a, b) => { const A = SORTERS[key](a), B = SORTERS[key](b); return A < B ? -dir : A > B ? dir : 0; });
    if (!items.length) {
      const noData = !Object.values(state.me.features.sources).some(Boolean);
      results(`<div class="empty">${emptyText || (noData
        ? "<h2>Источник данных ещё не подключён</h2><p>Как только поступят закупки с портала, подходящие появятся здесь и в Telegram.</p>"
        : state.query ? `<h2>Среди подобранных тендеров совпадений нет</h2><p>Попробуйте «Искать по всей базе».</p>`
          : `<h2>Подходящих открытых тендеров пока нет</h2><p>Расширьте направления в профиле компании — подбор станет шире.</p>`)}</div>`);
      return;
    }
    const th = (k, label, cls = "") => `<th class="${cls}" data-sort="${k}" aria-sort="${key === k ? (dir > 0 ? "ascending" : "descending") : "none"}">${label}${key === k ? `<span class="arrow">${dir > 0 ? "↑" : "↓"}</span>` : ""}</th>`;
    const rows = items.map((l) => {
      const left = timeLeft(l.hours_left);
      return `<tr data-id="${l.id}">
        <td>${matchCell(l.score)}</td>
        <td><div class="cell-title">${esc(l.title)}</div><div class="cell-sub">${esc(l.customer || "Заказчик не указан")}</div>
          ${l.stage ? `<div class="tags">${stageTag(l.stage)}</div>` : ""}</td>
        <td class="cell-sub">${esc(l.region || "—")}</td>
        <td class="right"><span class="amount">${esc(money(l.amount))}</span></td>
        <td><div class="date">${esc(dateShort(l.deadline))}</div><div class="left ${left.cls}">${esc(left.text)}</div></td>
      </tr>`;
    }).join("");
    const list = items.map((l) => {
      const left = timeLeft(l.hours_left);
      return `<a class="row" href="#lot/${l.id}">
        <div><div class="cell-title">${esc(l.title)}</div><div class="cell-sub">${esc(l.customer || "")}</div></div>
        ${matchCell(l.score)}
        <div class="row-foot"><span class="amount">${esc(moneyShort(l.amount))}</span><span class="left ${left.cls}">${esc(left.text)}</span></div>
      </a>`;
    }).join("");
    results(`<table class="grid">
      <thead><tr>${th("score", "Совпадение")}${th("title", "Закупка")}<th>Регион</th>${th("amount", "Сумма", "right")}${th("deadline", "Приём заявок до")}</tr></thead>
      <tbody>${rows}</tbody></table><div class="list">${list}</div>`);
    document.querySelectorAll("#results tbody tr").forEach((tr) => tr.addEventListener("click", () => (location.hash = "#lot/" + tr.dataset.id)));
    document.querySelectorAll("#results th[data-sort]").forEach((h) => h.addEventListener("click", () => {
      const k = h.dataset.sort;
      state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : k === "deadline" || k === "title" ? 1 : -1 };
      drawResults();
    }));
  }

  async function searchEverywhere(q) {
    if (q.length < 3) return toast("Введите не менее трёх символов");
    results(`<div>${'<div class="skeleton-row"></div>'.repeat(4)}</div>`);
    state.items = (await api("/search?q=" + encodeURIComponent(q))).items;
    drawResults("<h2>Ничего не найдено</h2><p>Попробуйте другие формулировки.</p>");
  }

  // ── Карточка тендера ────────────────────────────
  async function renderLot(id) {
    state.market = null;
    view.innerHTML = `<div class="loading"><span class="spinner"></span>Загрузка…</div>`;
    if (tg) { tg.BackButton.show(); tg.BackButton.onClick(goBack); }
    const data = await api("/lots/" + id);
    const l = data.lot;
    const left = timeLeft(l.hours_left);
    const leftPct = l.hours_left == null ? 100 : Math.max(2, Math.min(100, (l.hours_left / (14 * 24)) * 100));
    const docs = (l.documents || []).filter((d) => d.url);
    view.innerHTML = `
      <div class="crumbs"><a href="#feed">Закупки</a> / Карточка закупки</div>
      <header class="detail-head">
        <h1>${esc(l.title)}</h1>
        <p class="page-sub">${esc(l.customer || "Заказчик не указан")}</p>
        ${l.is_demo ? `<div class="tags"><span class="tag demo">Демонстрационная закупка</span></div>` : ""}
      </header>
      <div class="detail">
        <div>
          <section class="panel">
            <div class="panel-head"><h2>Сведения о закупке</h2></div>
            <div class="panel-body">
              <dl class="facts">
                <dt>Предмет</dt><dd>${esc(l.description || l.title)}</dd>
                <dt>Заказчик</dt><dd>${esc(l.customer || "—")}</dd>
                <dt>Регион</dt><dd>${esc(l.region || "—")}</dd>
                <dt>Способ закупки</dt><dd>${esc(l.method || "—")}</dd>
                <dt>Сумма</dt><dd>${esc(money(l.amount))}</dd>
                <dt>Приём заявок до</dt><dd>${esc(dateTime(l.deadline))}</dd>
                <dt>Источник</dt><dd>${l.url ? `<a href="${esc(l.url)}" target="_blank" rel="noopener">Открыть на портале</a>` : esc(l.source === "demo" ? "Демонстрационные данные" : l.source === "upload" ? "ТЗ, присланное в Telegram" : l.source)}</dd>
              </dl>
              ${l.reason ? `<p class="reason">Почему подходит: ${esc(l.reason)}</p>` : ""}
            </div>
          </section>

          <section class="panel" id="market-panel" hidden>
            <div class="panel-head"><h2>Рынок и конкуренты</h2><span class="hint" id="market-note"></span></div>
            <div id="market"></div>
          </section>

          <section class="panel" id="ready-panel" hidden>
            <div class="panel-head"><h2>Готовность к заявке</h2><span id="ready-sum" class="ready-sum"></span></div>
            <div id="ready"></div>
          </section>

          <section class="panel" id="gen-panel">
            <div class="panel-head"><h2>Документы заявки</h2><span class="hint">Word, готовы к правке и подписи</span></div>
            <div id="gen"></div>
          </section>

          <section class="panel" id="analysis-panel">
            <div class="panel-head"><h2>ИИ-разбор</h2><span id="analysis-action"></span></div>
            <div id="analysis"></div>
          </section>

          <section class="panel" id="ask-panel">
            <div class="panel-head"><h2>Вопрос по закупке</h2><span class="hint">Ответ по документации, с указанием источника</span></div>
            <div class="panel-body ask">
              <div class="ask-log" id="ask-log"></div>
              <div class="ask-chips">${["Предусмотрен ли аванс?", "Какие штрафы и пени по договору?", "Какой опыт требуется?", "Можно ли предложить аналог?"].map((q) => `<button class="chip" type="button">${q}</button>`).join("")}</div>
              <form class="ask-form" id="ask-form"><input class="input" name="q" maxlength="1000" placeholder="Например: какие сроки поставки и где место поставки?" autocomplete="off"><button class="btn btn-primary" type="submit">Спросить</button></form>
            </div>
          </section>

          ${docs.length ? `<section class="panel"><div class="panel-head"><h2>Документация</h2></div><div class="panel-body"><ul class="a-list">${docs.map((d) => `<li><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.name)}</a></li>`).join("")}</ul></div></section>` : ""}
        </div>

        <aside class="side">
          <section class="panel"><div class="panel-body">
            <div><span class="field-label">Сумма закупки</span><div class="big-amount">${esc(money(l.amount))}</div></div>
            <div class="term ${left.cls}"><span class="field-label">Срок подачи</span>
              <div class="term-bar"><i style="--left:${leftPct}"></i></div>
              <div class="term-text"><span>${esc(dateTime(l.deadline))}</span><strong>${esc(left.text)}</strong></div></div>
            <div><span class="field-label">Совпадение с профилем</span>${matchCell(l.score)}</div>
            <div><label class="field-label" for="stage">Статус в воронке</label>
              <select id="stage" class="select"><option value="">Не отслеживается</option>
                ${STAGES.map(([k, n]) => `<option value="${k}" ${l.stage === k ? "selected" : ""}>${n}</option>`).join("")}</select></div>
            <div><label class="field-label" for="note">Заметка</label>
              <textarea id="note" class="textarea" placeholder="Цена участия, контакты, что подготовить">${esc(data.note)}</textarea></div>
            <div class="form-actions"><button class="btn" data-act="hide">Скрыть из подборки</button></div>
          </div></section>
          ${calcPanel(l)}
        </aside>
      </div>`;

    renderAnalysis(id, data.analysis, data.ai);
    if (data.analysis) loadReadiness(id);
    bindCalc(l);
    loadMarket(l);
    bindAsk(id, data.ai);
    loadGen(id);
    const stage = view.querySelector("#stage");
    const note = view.querySelector("#note");
    stage.addEventListener("change", async () => {
      await api(`/lots/${id}/stage`, { method: "POST", body: { stage: stage.value || null, note: note.value } });
      haptic(); toast(stage.value ? `Статус: ${STAGE_NAME[stage.value]}` : "Закупка убрана из воронки");
    });
    note.addEventListener("change", async () => {
      if (!stage.value) stage.value = "saved";
      await api(`/lots/${id}/stage`, { method: "POST", body: { stage: stage.value, note: note.value } });
      toast("Заметка сохранена");
    });
    view.querySelector('[data-act="hide"]').addEventListener("click", async () => {
      await api(`/lots/${id}/hide`, { method: "POST" }); toast("Закупка скрыта из подборки"); goBack();
    });
  }

  const VERDICT = { go: "Рекомендуем участвовать", consider: "Требует оценки", skip: "Участие не рекомендуется" };

  function renderAnalysis(id, a, aiOn) {
    const box = document.getElementById("analysis");
    const action = document.getElementById("analysis-action");
    if (!a) {
      action.innerHTML = aiOn ? `<button class="btn btn-primary" data-act="analyze">Выполнить разбор</button>` : "";
      box.innerHTML = `<div class="panel-body"><p class="desc">${aiOn
        ? "Модель изучит условия закупки и документацию и подготовит заключение: соответствие вашему профилю, требования к поставщику, перечень документов и риски. Занимает 30–60 секунд."
        : "ИИ-модуль не подключён. После подключения здесь будет заключение по закупке: требования, документы и риски."}</p></div>`;
      const btn = action.querySelector("button");
      if (btn) btn.addEventListener("click", () => runAnalysis(id, false));
      return;
    }
    action.innerHTML = `<button class="btn" data-act="redo">Обновить</button>`;
    action.querySelector("button").addEventListener("click", () => runAnalysis(id, true));
    const key = "kyran_docs_" + id;
    let done = {};
    try { done = JSON.parse(localStorage.getItem(key) || "{}"); } catch (_) {}
    const sec = (title, body) => `<div class="a-section"><h3>${title}</h3>${body}</div>`;
    const none = `<p class="cell-sub">В документации не указано</p>`;
    const reqs = (a.requirements || []).length
      ? `<table class="a-table">${a.requirements.map((r) => `<tr><td>${esc(r.text || r)}</td><td class="src">${esc(r.source || "")}</td></tr>`).join("")}</table>` : none;
    const docsList = (a.documents || []).length
      ? a.documents.map((d, i) => `<label class="check"><input type="checkbox" data-doc="${i}" ${done[i] ? "checked" : ""}><span>${esc(d)}</span></label>`).join("") : none;
    const SEV = { high: "Высокий", medium: "Средний", low: "Низкий" };
    const risks = (a.risks || []).length
      ? `<table class="a-table">${a.risks.map((r) => `<tr><td style="width:84px"><span class="sev ${esc(r.level || "")}">${SEV[r.level] || "—"}</span></td><td>${esc(r.text || r)}</td></tr>`).join("")}</table>` : none;
    const ol = (arr) => `<ol class="a-list">${arr.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>`;
    box.innerHTML = `
      <div class="verdict ${esc(a.verdict)}"><span class="verdict-badge">${VERDICT[a.verdict] || VERDICT.consider}${a.score != null ? ` · ${esc(a.score)}%` : ""}</span><p>${esc(a.summary)}</p></div>
      ${a.scope && a.scope.length ? sec("Объём поставки / работ", ol(a.scope)) : ""}
      ${sec("Требования к поставщику", reqs)}
      ${sec("Документы для заявки", docsList)}
      ${sec("Риски", risks)}
      ${a.questions && a.questions.length ? sec("Вопросы заказчику", ol(a.questions)) : ""}
      ${a.next_steps && a.next_steps.length ? sec("Рекомендуемые действия", ol(a.next_steps)) : ""}
      <div class="a-foot">${a.docs_used && a.docs_used.length ? "Проанализированы документы: " + esc(a.docs_used.join(", ")) + ". " : "Заключение подготовлено по карточке закупки, документация не загружалась. "}Заключение носит справочный характер — сверяйте с первоисточником.</div>`;
    box.querySelectorAll("[data-doc]").forEach((c) => c.addEventListener("change", () => {
      done[c.dataset.doc] = c.checked;
      try { localStorage.setItem(key, JSON.stringify(done)); } catch (_) {}
    }));
  }

  async function runAnalysis(id, refresh) {
    const box = document.getElementById("analysis");
    document.getElementById("analysis-action").innerHTML = "";
    box.innerHTML = `<div class="loading"><span class="spinner"></span>Анализ условий закупки и документации…</div>`;
    try {
      const { analysis } = await api(`/lots/${id}/analyze?refresh=${refresh}`, { method: "POST" });
      haptic(); renderAnalysis(id, analysis, true); loadReadiness(id); loadGen(id); loadMe().catch(() => {});
    } catch (e) {
      box.innerHTML = `<div class="panel-body"><p class="desc">${esc(e.message)}</p>${e.status === 402 ? `<p style="margin-top:12px"><a class="btn btn-primary" href="#profile/plan">Выбрать тариф</a></p>` : ""}</div>`;
      if (e.status !== 402) {
        document.getElementById("analysis-action").innerHTML = `<button class="btn">Повторить</button>`;
        document.querySelector("#analysis-action button").addEventListener("click", () => runAnalysis(id, refresh));
      }
    }
  }


  // ── Готовность к заявке ─────────────────────────
  const READY = { ready: ["ok", "Есть"], expiring: ["warn", "Истекает"], expired: ["bad", "Просрочен"], missing: ["bad", "Нет в библиотеке"] };
  async function loadReadiness(id) {
    const panel = document.getElementById("ready-panel");
    if (!panel) return;
    let r;
    try { r = await api(`/lots/${id}/readiness`); } catch (_) { return; }
    if (!r.total) return;
    panel.hidden = false;
    const pct = Math.round((r.ready / r.total) * 100);
    document.getElementById("ready-sum").innerHTML = `<span class="ready-num">${r.ready} из ${r.total}</span><span class="match-bar"><i style="--p:${pct}"></i></span>`;
    document.getElementById("ready").innerHTML = `<table class="a-table ready-table">${r.items.map((x) => {
      const [cls, label] = READY[x.status] || READY.missing;
      const doc = x.document ? `<div class="cell-sub">${esc(x.document.title)}${x.document.expires_at ? " · до " + esc(dateShort(x.document.expires_at)) : ""}</div>` : "";
      return `<tr><td>${esc(x.requirement)}${doc}</td><td class="right"><span class="status ${cls}">${label}</span></td></tr>`;
    }).join("")}</table>
    <div class="a-foot">Документы сопоставлены с вашей библиотекой автоматически, срок действия проверен на дату окончания приёма заявок.
      <a href="#documents">Открыть библиотеку</a></div>`;
  }



  // ── Документы заявки ────────────────────────────
  async function downloadGenerated(d) {
    // В Telegram скачивание файлов из мини-приложения ненадёжно — бот присылает документ в чат
    if (tg) {
      try { await api(`/generated/${d.id}/send`, { method: "POST" }); toast("Документ отправлен в чат с ботом"); }
      catch (e) { toast(e.message); }
      return;
    }
    const res = await fetch(`/api/generated/${d.id}/file`, { headers: { Authorization: authHeader() } });
    if (!res.ok) return toast("Не удалось скачать документ");
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = d.filename; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  async function loadGen(id) {
    const box = document.getElementById("gen");
    if (!box) return;
    let r;
    try { r = await api(`/lots/${id}/docs`); } catch (e) { box.innerHTML = `<div class="panel-body"><p class="desc">${esc(e.message)}</p></div>`; return; }
    const p = state.me.plan;
    box.innerHTML = `
      ${r.missing.length ? `<div class="gen-warn">Для документов не хватает реквизитов: ${esc(r.missing.join(", "))}. Пустые места останутся прочерками. <a href="#profile/company">Заполнить реквизиты</a></div>` : ""}
      <div class="gen-list">${r.kinds.map((k) => `
        <div class="gen-row" data-kind="${k.code}">
          <div class="gen-ico" aria-hidden="true"><svg viewBox="0 0 20 20"><path d="M5 2.5h7l3.5 3.5v11.5H5z"/><path d="M12 2.5V6h3.5"/></svg></div>
          <div class="gen-body"><div class="cell-title">${esc(k.title)}${k.ai ? ` <span class="tag-ai">ИИ</span>` : ""}</div>
            <div class="cell-sub">${k.doc ? `Сформирован ${esc(dateTime(k.doc.created_at))}` : esc(k.description)}</div></div>
          <div class="gen-act">${k.doc ? `<button class="btn btn-sm" data-dl="${k.doc.id}">${tg ? "В чат" : "Скачать"}</button>${tg ? "" : `<button class="btn btn-sm btn-ghost" data-tg="${k.doc.id}" title="Отправить в Telegram">В Telegram</button>`}<button class="btn btn-sm btn-ghost" data-make="${k.code}">Обновить</button>`
            : `<button class="btn btn-sm ${k.code === "tech" ? "btn-primary" : ""}" data-make="${k.code}" ${k.ai && !r.ai ? "disabled title='Нужен ИИ-модуль'" : ""}>Сформировать</button>`}</div>
        </div>`).join("")}</div>
      <div class="a-foot">Документы с отметкой ИИ расходуют лимит тарифа: ${p.usage.generations ?? 0} из ${p.limits.generations ?? 0} в этом месяце. Перед подачей проверьте и подпишите.</div>`;
    const byId = {};
    r.kinds.forEach((k) => { if (k.doc) byId[k.doc.id] = k.doc; });
    box.querySelectorAll("[data-dl]").forEach((b) => b.addEventListener("click", () => downloadGenerated(byId[b.dataset.dl])));
    box.querySelectorAll("[data-tg]").forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/generated/${b.dataset.tg}/send`, { method: "POST" }); toast("Отправлено в чат с ботом"); } catch (e) { toast(e.message); }
    }));
    box.querySelectorAll("[data-make]").forEach((b) => b.addEventListener("click", async () => {
      const row = b.closest(".gen-row");
      row.querySelector(".gen-act").innerHTML = `<span class="gen-wait"><span class="spinner"></span>${b.dataset.make === "tech" ? "Готовим, до минуты…" : "Готовим…"}</span>`;
      try {
        const d = await api(`/lots/${id}/docs/${b.dataset.make}`, { method: "POST" });
        haptic(); toast(`«${d.title}» готов`);
        await loadMe().catch(() => {});
        await loadGen(id);
        downloadGenerated(d);
      } catch (e) {
        toast(e.message);
        if (e.status === 402) location.hash = "#profile/plan"; else loadGen(id);
      }
    }));
  }

  // ── Рынок и конкуренты ──────────────────────────
  async function loadMarket(l) {
    let r;
    try { r = await api(`/lots/${l.id}/insights`); } catch (_) { return; }
    if (!r.available) return;
    state.market = r;
    const panel = document.getElementById("market-panel");
    if (!panel) return;
    panel.hidden = false;
    document.getElementById("market-note").textContent = `${r.similar_count} ${plural(r.similar_count, "похожая закупка", "похожие закупки", "похожих закупок")}${r.is_demo ? " · демо-данные" : ""}`;
    const p = r.price;
    const max = Math.max(1, ...r.histogram.counts);
    const hist = r.histogram.counts.map((c, i) => `<div class="hbar" title="${i * 5}–${i === 6 ? "…" : i * 5 + 5}%: ${c}"><i style="--h:${(c / max) * 100}"></i><span>${i === 6 ? "30+" : i * 5}</span></div>`).join("");
    const cust = r.customer;
    const warn = [];
    if (cust.loyal_winner) warn.push(`У заказчика устойчивый поставщик: <b>${esc(cust.loyal_winner.name)}</b> выиграл ${cust.loyal_winner.wins} из ${cust.done} закупок.`);
    if (cust.failed) warn.push(`${cust.failed} ${plural(cust.failed, "закупка", "закупки", "закупок")} заказчика ${plural(cust.failed, "не состоялась", "не состоялись", "не состоялись")} — часто из-за единственной заявки.`);
    if (r.single_share >= 30) warn.push(`В ${r.single_share}% похожих закупок была одна заявка — шанс выиграть без снижения.`);
    document.getElementById("market").innerHTML = `
      <div class="mkt-kpis">
        <div><span class="field-label">Снижение цены</span><b>${p ? p.median_discount + "%" : "—"}</b><small>${p ? `обычно ${p.low_discount}–${p.high_discount}%` : "нет данных"}</small></div>
        <div><span class="field-label">Участников</span><b>${r.avg_participants ?? "—"}</b><small>в среднем на закупку</small></div>
        <div><span class="field-label">Рекомендуемая цена</span><b>${p ? esc(moneyShort(p.recommended)) : "—"}</b><small>${p ? `${esc(moneyShort(p.range[0]))} – ${esc(moneyShort(p.range[1]))}` : ""}</small></div>
      </div>
      <div class="mkt-grid">
        <div><h3 class="mkt-h">Распределение снижения, %</h3><div class="hist">${hist}</div></div>
        <div><h3 class="mkt-h">Кто выигрывает похожие закупки</h3>
          ${r.competitors.length ? `<table class="a-table mkt-table">${r.competitors.map((c) => `<tr><td>${c.bin ? `<a href="#rival/${esc(c.bin)}">${esc(c.name)}</a>` : esc(c.name)}</td><td class="right nowrap">${c.wins} ${plural(c.wins, "победа", "победы", "побед")}</td><td class="right nowrap cell-sub">${c.avg_discount ? "−" + c.avg_discount + "%" : c.avg_discount === 0 ? "без снижения" : ""}</td></tr>`).join("")}</table>` : `<p class="cell-sub">Нет данных</p>`}</div>
      </div>
      ${warn.length ? `<div class="mkt-warn">${warn.map((w) => `<p>${w}</p>`).join("")}</div>` : ""}
      ${cust.recent.length ? `<details class="mkt-more"><summary>История заказчика: ${cust.total} ${plural(cust.total, "закупка", "закупки", "закупок")}</summary>
        <table class="a-table">${cust.recent.map((a) => `<tr><td>${esc(a.title)}<div class="cell-sub">${esc(dateShort(a.date))} · ${a.status === "failed" ? "не состоялась" : esc(a.winner)}${a.participants ? " · участников: " + a.participants : ""}</div></td>
          <td class="right nowrap"><span class="amount">${esc(moneyShort(a.price || a.amount))}</span>${a.discount != null ? `<div class="cell-sub">−${a.discount}%</div>` : ""}</td></tr>`).join("")}</table></details>` : ""}`;
    bindCalc(l); // пересчитать с рыночной ценой
  }

  // ── Вопросы по закупке ──────────────────────────
  function bindAsk(id, aiOn) {
    const log = document.getElementById("ask-log"), form = document.getElementById("ask-form");
    if (!form) return;
    const history = [];
    if (!aiOn) log.innerHTML = `<p class="cell-sub">Ответы по документации заработают после подключения ИИ-модуля.</p>`;
    const send = async (q) => {
      q = q.trim(); if (q.length < 2) return;
      log.insertAdjacentHTML("beforeend", `<div class="msg me">${esc(q)}</div><div class="msg ai pending"><span class="spinner"></span>Ищу ответ в документации…</div>`);
      form.q.value = "";
      const pending = log.querySelector(".pending");
      try {
        const { answer } = await api(`/lots/${id}/ask`, { method: "POST", body: { question: q, history } });
        history.push({ role: "user", content: q }, { role: "assistant", content: answer });
        pending.classList.remove("pending"); pending.textContent = answer;
      } catch (e) { pending.classList.remove("pending"); pending.classList.add("err"); pending.textContent = e.message; }
    };
    form.addEventListener("submit", (e) => { e.preventDefault(); send(form.q.value); });
    document.querySelectorAll(".ask-chips .chip").forEach((c) => c.addEventListener("click", () => send(c.textContent)));
  }

  // ── Калькулятор цены участия ────────────────────
  function calcPanel(l) {
    if (!l.amount) return "";
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem("kyran_calc_" + l.id) || "{}"); } catch (_) {}
    const v = (k, d) => saved[k] ?? d;
    return `<section class="panel"><div class="panel-head"><h2>Расчёт цены</h2></div><div class="panel-body calc">
      <div class="calc-grid">
        <label><span class="field-label">Себестоимость, ₸</span><input class="input" data-c="cost" inputmode="numeric" value="${esc(v("cost", ""))}" placeholder="${esc(Math.round(l.amount * 0.75).toLocaleString("ru-RU"))}"></label>
        <label><span class="field-label">Наценка, %</span><input class="input" data-c="markup" inputmode="decimal" value="${esc(v("markup", 15))}"></label>
        <label><span class="field-label">Обеспечение заявки, %</span><input class="input" data-c="bid" inputmode="decimal" value="${esc(v("bid", 1))}"></label>
        <label><span class="field-label">Обеспечение договора, %</span><input class="input" data-c="contract" inputmode="decimal" value="${esc(v("contract", 3))}"></label>
      </div>
      <dl class="calc-out" id="calc-out"></dl>
      <p class="hint">Проценты обеспечения проверяйте в документации конкретной закупки.</p>
    </div></section>`;
  }
  function bindCalc(l) {
    const out = document.getElementById("calc-out");
    if (!out) return;
    const num = (x) => parseFloat(String(x).replace(/\s/g, "").replace(",", ".")) || 0;
    const calc = () => {
      const vals = {};
      view.querySelectorAll("[data-c]").forEach((i) => (vals[i.dataset.c] = i.value));
      try { localStorage.setItem("kyran_calc_" + l.id, JSON.stringify(vals)); } catch (_) {}
      const cost = num(vals.cost), price = cost * (1 + num(vals.markup) / 100);
      if (!cost) { out.innerHTML = `<dt>Введите себестоимость</dt><dd></dd>`; return; }
      const drop = l.amount ? (1 - price / l.amount) * 100 : 0;
      const over = price > l.amount;
      const mk = state.market && state.market.price;
      const mkRows = mk ? `<dt class="sep">Цена по рынку</dt><dd class="sep">${esc(money(mk.recommended))}</dd>
        <dt>Прибыль по рынку</dt><dd class="${mk.recommended < cost ? "bad" : ""}">${esc(money(mk.recommended - cost))}</dd>
        <dt>Относительно рынка</dt><dd class="${price > mk.range[1] ? "bad" : ""}">${price > mk.range[1] ? "выше цен победителей" : price < mk.range[0] ? "ниже рынка" : "в диапазоне победителей"}</dd>` : "";
      out.innerHTML = `
        <dt>Ваша цена</dt><dd class="${over ? "bad" : ""}">${esc(money(price))}</dd>
        <dt>Снижение</dt><dd class="${over ? "bad" : ""}">${over ? "выше суммы закупки" : drop.toFixed(1) + "%"}</dd>
        <dt>Прибыль</dt><dd>${esc(money(price - cost))}</dd>
        <dt>Обеспечение заявки</dt><dd>${esc(money(l.amount * num(vals.bid) / 100))}</dd>
        <dt>Обеспечение договора</dt><dd>${esc(money(price * num(vals.contract) / 100))}</dd>${mkRows}`;
    };
    view.querySelectorAll("[data-c]").forEach((i) => { i.oninput = calc; });
    calc();
  }

  // ── Документы ───────────────────────────────────
  const DOC_STATUS = { valid: ["ok", "Действует"], expiring: ["warn", "Истекает"], expired: ["bad", "Просрочен"], no_term: ["", "Бессрочный"] };
  function fileSize(b) { return b > 1048576 ? (b / 1048576).toFixed(1) + " МБ" : b ? Math.max(1, Math.round(b / 1024)) + " КБ" : ""; }

  async function renderDocuments() {
    view.innerHTML = `<div class="loading"><span class="spinner"></span>Загрузка…</div>`;
    const { items, kinds } = await api("/documents");
    const p = state.me.plan;
    const bad = items.filter((d) => d.status === "expired").length, warn = items.filter((d) => d.status === "expiring").length;
    view.innerHTML = `
      <div class="page-head"><div><h1>Документы компании</h1>
        <p class="page-sub">Справки, лицензии, сертификаты и подтверждение опыта в одном месте. Kyran сверяет их с требованиями каждой закупки и напоминает в Telegram за 30 и 7 дней до окончания срока.</p></div>
        <button class="btn btn-primary" id="add-doc">Добавить документ</button></div>
      <section class="kpis kpis-3">
        <div class="kpi"><div class="kpi-label">В библиотеке</div><div class="kpi-value">${items.length}<small> из ${p.limits.documents}</small></div></div>
        <div class="kpi"><div class="kpi-label">Истекают в течение 30 дней</div><div class="kpi-value ${warn ? "warn" : ""}">${warn}</div></div>
        <div class="kpi"><div class="kpi-label">Просрочены</div><div class="kpi-value ${bad ? "bad" : ""}">${bad}</div></div>
      </section>
      <section class="panel" id="doc-form-panel" hidden></section>
      <div class="table-wrap">${items.length ? `<table class="grid docs stack">
        <thead><tr><th>Документ</th><th>Тип</th><th>Действует до</th><th>Статус</th><th class="right"></th></tr></thead>
        <tbody>${items.map((d) => { const [cls, label] = DOC_STATUS[d.status]; return `<tr>
          <td><div class="cell-title">${esc(d.title)}</div><div class="cell-sub">${d.filename ? esc(d.filename) + " · " + fileSize(d.size) : "Без файла"}</div></td>
          <td class="cell-sub">${esc(d.kind_name)}</td>
          <td><div class="date">${d.expires_at ? esc(dateShort(d.expires_at)) : "—"}</div></td>
          <td><span class="status ${cls}">${label}</span></td>
          <td class="right nowrap">${d.filename ? `<button class="btn btn-sm" data-dl="${d.id}">Скачать</button>` : ""}
            <button class="btn btn-sm" data-edit="${d.id}">Изменить</button> <button class="btn btn-sm btn-ghost" data-del="${d.id}" aria-label="Удалить">Удалить</button></td></tr>`; }).join("")}</tbody></table>`
        : `<div class="empty"><h2>Библиотека пуста</h2><p>Загрузите справку об отсутствии налоговой задолженности, свидетельство о регистрации, лицензии и акты выполненных работ — и Kyran будет показывать готовность к каждой закупке.</p></div>`}</div>`;

    const formPanel = view.querySelector("#doc-form-panel");
    const openForm = (doc) => {
      formPanel.hidden = false;
      const val = (k) => esc(doc ? (k.endsWith("_at") ? (doc[k] || "").slice(0, 10) : doc[k]) : "");
      formPanel.innerHTML = `<div class="panel-head"><h2>${doc ? "Изменить документ" : "Новый документ"}</h2></div><form class="panel-body form" id="doc-form">
        <div class="form-grid">
          <label class="form-row"><span class="title">Название</span><input class="input" name="title" required maxlength="300" value="${val("title")}" placeholder="Справка об отсутствии налоговой задолженности"></label>
          <label class="form-row"><span class="title">Тип</span><select class="select" name="kind">${Object.entries(kinds).map(([k, n]) => `<option value="${k}" ${doc && doc.kind === k ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>
          <label class="form-row"><span class="title">Дата выдачи</span><input class="input" type="date" name="issued_at" value="${val("issued_at")}"></label>
          <label class="form-row"><span class="title">Действует до</span><input class="input" type="date" name="expires_at" value="${val("expires_at")}"><span class="hint">Оставьте пустым для бессрочных</span></label>
        </div>
        ${doc ? "" : `<label class="form-row"><span class="title">Файл</span><input class="input" type="file" name="file" accept=".pdf,.doc,.docx,.xls,.xlsx,.jpg,.jpeg,.png,.zip,.rar,.txt"><span class="hint">PDF, Word, Excel, изображения или архив, до 15 МБ</span></label>`}
        <label class="form-row"><span class="title">Комментарий</span><input class="input" name="note" maxlength="2000" value="${val("note")}" placeholder="Например: номер лицензии, вид работ"></label>
        <div class="form-actions"><button class="btn btn-primary" type="submit">${doc ? "Сохранить" : "Загрузить"}</button><button class="btn" type="button" data-cancel>Отмена</button></div>
      </form>`;
      formPanel.querySelector("[data-cancel]").onclick = () => (formPanel.hidden = true);
      formPanel.querySelector("form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const fd = new FormData(e.target), btn = e.target.querySelector("[type=submit]");
        btn.disabled = true;
        try {
          if (doc) await api("/documents/" + doc.id, { method: "PUT", body: Object.fromEntries(fd) });
          else await api("/documents", { method: "POST", body: fd });
          toast(doc ? "Документ сохранён" : "Документ добавлен"); await loadMe(); renderDocuments();
        } catch (err) { toast(err.message); btn.disabled = false; }
      });
      formPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    view.querySelector("#add-doc").addEventListener("click", () => openForm(null));
    view.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => openForm(items.find((d) => d.id == b.dataset.edit))));
    view.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm("Удалить документ из библиотеки?")) return;
      await api("/documents/" + b.dataset.del, { method: "DELETE" }); toast("Документ удалён"); loadMe(); renderDocuments();
    }));
    view.querySelectorAll("[data-dl]").forEach((b) => b.addEventListener("click", async () => {
      const res = await fetch(`/api/documents/${b.dataset.dl}/file`, { headers: { Authorization: authHeader() } });
      if (!res.ok) return toast("Не удалось скачать файл");
      const d = items.find((x) => x.id == b.dataset.dl);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(await res.blob()); a.download = d.filename || "document"; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    }));
  }



  // ── Конкуренты ──────────────────────────────────
  async function followBin(bin, name, btn) {
    if (btn) btn.disabled = true;
    try { await api("/competitors", { method: "POST", body: { bin, name: name || "" } }); haptic(); toast("Слежу за конкурентом — сообщу о новых победах"); return true; }
    catch (e) { toast(e.message); if (btn) btn.disabled = false; if (e.message.includes("лимит")) location.hash = "#profile/plan"; return false; }
  }

  async function renderRivals() {
    view.innerHTML = `<div class="loading"><span class="spinner"></span>Загрузка…</div>`;
    const { items, suggest } = await api("/competitors");
    const p = state.me.plan;
    view.innerHTML = `
      <div class="page-head"><div><h1>Конкуренты</h1>
        <p class="page-sub">Следите за компаниями, которые выигрывают в ваших направлениях: Kyran сообщит в Telegram о каждой их победе — что, у кого и по какой цене.</p></div></div>
      <form class="toolbar" id="follow-form">
        <input class="input" style="max-width:240px" name="bin" inputmode="numeric" maxlength="12" placeholder="БИН, 12 цифр" required aria-label="БИН конкурента">
        <input class="input" style="flex:1;min-width:200px" name="name" maxlength="200" placeholder="Название (необязательно)" aria-label="Название">
        <button class="btn btn-primary" type="submit">Следить</button>
        <span class="hint" style="margin:0">${items.length} из ${p.limits.competitors} по тарифу</span>
      </form>
      ${items.length ? `<div class="rival-grid">${items.map((x) => `
        <a class="rival" href="#rival/${x.bin}">
          <div class="rival-top"><span class="avatar">${esc(initials({ company_name: x.name }))}</span><div><div class="cell-title">${esc(x.name)}</div><div class="cell-sub">БИН ${esc(x.bin)}</div></div></div>
          <dl class="rival-stats"><div><dt>Побед за год</dt><dd>${x.wins}</dd></div><div><dt>Сумма</dt><dd>${esc(moneyShort(x.total))}</dd></div><div><dt>Последняя</dt><dd>${x.last_win ? esc(dateShort(x.last_win)) : "—"}</dd></div></dl>
        </a>`).join("")}</div>`
        : `<div class="empty"><h2>Вы пока ни за кем не следите</h2><p>Добавьте БИН конкурента или выберите из тех, кто чаще всего выигрывает в ваших направлениях.</p></div>`}
      ${suggest.length ? `<section class="panel" style="margin-top:20px"><div class="panel-head"><h2>Чаще всего выигрывают в ваших направлениях</h2><span class="hint">за полгода</span></div>
        <table class="grid stack"><tbody>${suggest.map((x) => `<tr><td><div class="cell-title">${esc(x.name)}</div><div class="cell-sub">БИН ${esc(x.bin)}</div></td>
          <td class="right nowrap">${x.wins} ${plural(x.wins, "победа", "победы", "побед")}</td><td class="right nowrap"><span class="amount">${esc(moneyShort(x.total))}</span></td>
          <td class="right nowrap"><a class="btn btn-sm" href="#rival/${x.bin}">Досье</a> <button class="btn btn-sm btn-primary" data-follow="${x.bin}" data-name="${esc(x.name)}">Следить</button></td></tr>`).join("")}</tbody></table></section>` : ""}`;
    view.querySelector("#follow-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const bin = String(fd.get("bin")).replace(/\D/g, "");
      if (bin.length !== 12) return toast("БИН — 12 цифр");
      if (await followBin(bin, fd.get("name"))) renderRivals();
    });
    view.querySelectorAll("[data-follow]").forEach((b) => b.addEventListener("click", async () => { if (await followBin(b.dataset.follow, b.dataset.name, b)) renderRivals(); }));
  }

  async function renderRival(bin) {
    view.innerHTML = `<div class="loading"><span class="spinner"></span>Загрузка…</div>`;
    if (tg) { tg.BackButton.show(); tg.BackButton.onClick(goBack); }
    const d = await api(`/competitors/${bin}`);
    const maxM = Math.max(1, ...d.months.map((m) => m.total));
    const MON = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
    const list = (arr) => arr.length ? `<ol class="rank">${arr.map((x) => `<li><span>${esc(x.name)}</span><b>${x.wins}</b></li>`).join("")}</ol>` : `<p class="cell-sub">Нет данных</p>`;
    view.innerHTML = `
      <div class="crumbs"><a href="#rivals">Конкуренты</a> / Досье</div>
      <div class="page-head"><div class="profile-head" style="margin:0"><span class="avatar xl">${esc(initials({ company_name: d.name || "?" }))}</span>
        <div><h1>${esc(d.name || "Компания")}</h1><p class="page-sub">БИН ${esc(d.bin)}${d.is_demo ? " · демонстрационные данные" : ""}</p></div></div>
        <div>${d.following ? `<button class="btn" id="unfollow">Не следить</button>` : `<button class="btn btn-primary" id="follow">Следить</button>`}</div></div>
      <section class="kpis">
        <div class="kpi"><div class="kpi-label">Побед за год</div><div class="kpi-value">${d.wins}</div></div>
        <div class="kpi"><div class="kpi-label">Сумма договоров</div><div class="kpi-value">${esc(moneyShort(d.total))}</div></div>
        <div class="kpi"><div class="kpi-label">Снижение цены</div><div class="kpi-value">${d.median_discount != null ? d.median_discount + "%" : "—"}</div></div>
        <div class="kpi"><div class="kpi-label">В ваших направлениях</div><div class="kpi-value ${d.overlap.wins ? "bad" : ""}">${d.overlap.wins}${d.overlap.total ? `<small> · ${esc(moneyShort(d.overlap.total))}</small>` : ""}</div></div>
      </section>
      <section class="panel"><div class="panel-head"><h2>Победы по месяцам</h2><span class="hint">сумма договоров</span></div><div class="panel-body">
        <div class="months">${d.months.map((m) => { const [y, mo] = m.month.split("-"); return `<div class="mbar" title="${MON[+mo - 1]} ${y}: ${esc(money(m.total))}"><i style="--h:${(m.total / maxM) * 100}"></i><span>${MON[+mo - 1]}</span></div>`; }).join("")}</div>
      </div></section>
      <div class="report-grid" style="margin-top:16px">
        <section class="panel"><div class="panel-head"><h2>Главные заказчики</h2></div><div class="panel-body">${list(d.customers)}</div></section>
        <section class="panel"><div class="panel-head"><h2>Регионы</h2></div><div class="panel-body">${list(d.regions)}</div></section>
      </div>
      <section class="panel" style="margin-top:16px"><div class="panel-head"><h2>Последние победы</h2></div>
        ${d.recent.length ? `<table class="grid stack"><thead><tr><th>Закупка</th><th>Дата</th><th class="right">Цена договора</th><th class="right">Снижение</th><th>Участников</th></tr></thead>
          <tbody>${d.recent.map((a) => `<tr><td><div class="cell-title">${esc(a.title)}</div><div class="cell-sub">${esc(a.customer)}</div></td><td class="cell-sub">${esc(dateShort(a.date))}</td>
            <td class="right"><span class="amount">${esc(money(a.price))}</span></td><td class="right">${a.discount != null ? a.discount + "%" : "—"}</td><td>${a.participants ?? "—"}</td></tr>`).join("")}</tbody></table>`
          : `<div class="empty"><p>За год побед этой компании в данных нет.</p></div>`}</section>`;
    const f = view.querySelector("#follow"), u = view.querySelector("#unfollow");
    if (f) f.addEventListener("click", async () => { if (await followBin(d.bin, d.name, f)) renderRival(bin); });
    if (u) u.addEventListener("click", async () => { await api(`/competitors/${d.bin}`, { method: "DELETE" }); toast("Больше не следим"); renderRival(bin); });
  }

  // ── Отчёт недели ────────────────────────────────
  async function renderReport() {
    view.innerHTML = `<div class="loading"><span class="spinner"></span>Загрузка…</div>`;
    const days = state.reportDays || 7;
    const r = await api("/report/weekly?days=" + days);
    const m = r.missed, st = r.activity.stages;
    const maxW = Math.max(1, ...r.winners.map((w) => w.total));
    view.innerHTML = `
      <div class="crumbs"><a href="#feed">Закупки</a> / Отчёт</div>
      <div class="page-head"><div><h1>Что прошло мимо вас</h1>
        <p class="page-sub">Итоги подходящих вам закупок, которые выиграли другие. Помогает понять, где вы теряете деньги и с кем конкурируете.${r.is_demo ? " По демонстрационным данным." : ""}</p></div>
        <div class="segmented" role="group" aria-label="Период">${[[7, "7 дней"], [30, "30 дней"], [90, "90 дней"]].map(([d, n]) => `<button data-days="${d}" aria-pressed="${d === days}">${n}</button>`).join("")}</div></div>
      <section class="kpis">
        <div class="kpi"><div class="kpi-label">Ушло конкурентам</div><div class="kpi-value bad">${esc(moneyShort(m.total))}</div></div>
        <div class="kpi"><div class="kpi-label">Подходящих закупок</div><div class="kpi-value">${m.count}</div></div>
        <div class="kpi"><div class="kpi-label">Среднее снижение у победителей</div><div class="kpi-value">${m.median_discount != null ? m.median_discount + "%" : "—"}</div></div>
        <div class="kpi"><div class="kpi-label">Ваши победы</div><div class="kpi-value ${r.won.count ? "ok" : ""}">${r.won.count}${r.won.total ? `<small> · ${esc(moneyShort(r.won.total))}</small>` : ""}</div></div>
      </section>
      <div class="report-grid">
        <section class="panel"><div class="panel-head"><h2>Кто забирает ваши закупки</h2></div><div class="panel-body">
          ${r.winners.length ? r.winners.map((w) => `<div class="wbar"><div class="wbar-top"><span>${w.bin ? `<a href="#rival/${esc(w.bin)}">${esc(w.name)}</a>` : esc(w.name)}</span><b>${esc(moneyShort(w.total))}</b></div>
            <div class="wbar-line"><i style="--p:${(w.total / maxW) * 100}"></i></div><div class="cell-sub">${w.wins} ${plural(w.wins, "победа", "победы", "побед")}</div></div>`).join("")
            : `<p class="cell-sub">За период подходящих итогов нет.</p>`}
        </div></section>
        <section class="panel"><div class="panel-head"><h2>Ваша активность</h2></div><div class="panel-body">
          <dl class="facts">
            <dt>Новых подходящих</dt><dd>${r.activity.new_matches}</dd>
            <dt>В подготовке</dt><dd>${st.preparing || 0}</dd>
            <dt>Подано заявок</dt><dd>${st.submitted || 0}</dd>
            <dt>Побед</dt><dd>${st.won || 0}</dd>
          </dl>
          ${r.soon.length ? `<h3 class="mkt-h" style="margin-top:16px">Не упустите — закрываются в ближайшие дни</h3>
            ${r.soon.map((x) => { const h = (new Date(x.deadline) - Date.now()) / 36e5; const left = timeLeft(h);
              return `<a class="soon" href="#lot/${x.id}"><span class="cell-title">${esc(x.title)}</span><span class="cell-sub">${esc(moneyShort(x.amount))} · <span class="left ${left.cls}">${esc(left.text)}</span></span></a>`; }).join("")}` : ""}
        </div></section>
      </div>
      <section class="panel" style="margin-top:16px"><div class="panel-head"><h2>Крупнейшие закупки, которые ушли другим</h2></div>
        ${m.items.length ? `<table class="grid stack"><thead><tr><th>Закупка</th><th>Победитель</th><th class="right">Цена договора</th><th class="right">Снижение</th><th>Участников</th></tr></thead>
          <tbody>${m.items.map((a) => `<tr><td><div class="cell-title">${esc(a.title)}</div><div class="cell-sub">${esc(a.customer)} · ${esc(dateShort(a.date))}</div></td>
            <td>${esc(a.winner)}</td><td class="right"><span class="amount">${esc(money(a.price))}</span></td>
            <td class="right">${a.discount != null ? a.discount + "%" : "—"}</td><td>${a.participants ?? "—"}</td></tr>`).join("")}</tbody></table>`
          : `<div class="empty"><p>За выбранный период подходящих итогов нет. Попробуйте период больше или расширьте направления в профиле.</p></div>`}
      </section>
      <p class="hint" style="margin-top:12px">Отчёт за неделю приходит в Telegram по понедельникам. В боте — команда /report.</p>`;
    view.querySelectorAll("[data-days]").forEach((b) => b.addEventListener("click", () => { state.reportDays = +b.dataset.days; renderReport(); }));
  }

  // ── Разборы ─────────────────────────────────────
  async function renderAnalyses() {
    view.innerHTML = `<div class="loading"><span class="spinner"></span>Загрузка…</div>`;
    const { items } = await api("/analyses");
    const p = state.me.plan;
    view.innerHTML = `
      <div class="page-head"><div><h1>ИИ-разборы</h1>
        <p class="page-sub">Заключения по закупкам: требования, документы, риски. В этом месяце использовано ${p.usage.analyses} из ${p.limits.analyses}.</p></div></div>
      <div class="table-wrap">${items.length ? `<table class="grid stack">
        <thead><tr><th>Заключение</th><th>Закупка</th><th class="right">Сумма</th><th>Дата разбора</th></tr></thead>
        <tbody>${items.map((x) => `<tr data-id="${x.lot.id}">
          <td><span class="verdict-chip ${esc(x.verdict)}">${VERDICT[x.verdict] || VERDICT.consider}</span>${x.score != null ? `<div class="cell-sub">${esc(x.score)}%</div>` : ""}</td>
          <td><div class="cell-title">${esc(x.lot.title)}</div><div class="cell-sub clamp">${esc(x.summary)}</div></td>
          <td class="right"><span class="amount">${esc(moneyShort(x.lot.amount))}</span></td>
          <td class="cell-sub">${esc(dateShort(x.created_at))}</td></tr>`).join("")}</tbody></table>`
        : `<div class="empty"><h2>Разборов пока нет</h2><p>Откройте любую закупку и нажмите «Выполнить разбор» — заключение сохранится здесь.</p></div>`}</div>`;
    view.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => (location.hash = "#lot/" + tr.dataset.id)));
  }

  // ── Вход по коду ────────────────────────────────
  let pollTimer = null;
  async function renderLogin(message) {
    clearInterval(pollTimer);
    document.body.classList.add("auth");
    view.innerHTML = `<div class="login">
      <div class="login-card">
        <svg class="brand-mark big" viewBox="20 44 200 144" aria-hidden="true"><g fill="currentColor"><path d="M112 118 L84 112 L52 90 L22 54 L40 62 L34 50 L54 62 L50 48 L70 66 L70 54 L88 76 L112 98 Z"/><path d="M128 118 L156 112 L188 90 L218 54 L200 62 L206 50 L186 62 L190 48 L170 66 L170 54 L152 76 L128 98 Z"/><path d="M107 96 L133 96 L136 132 L120 150 L104 132 Z"/><path d="M106 140 L120 152 L134 140 L140 178 L130 170 L120 184 L110 170 L100 178 Z"/><path d="M110 104 L110 82 L118 72 L130 72 L138 78 L146 82 L140 86 L136 84 L132 92 L131 104 Z"/></g></svg>
        <h1>Вход в Kyran</h1>
        <p class="lead">${esc(message || "Вход без пароля — через Telegram. Код подтверждает, что аккаунт ваш.")}</p>
        <div id="login-box"><div class="loading"><span class="spinner"></span>Получаем код…</div></div>
      </div>
      <p class="login-foot">Kyran — подбор и подготовка к участию в госзакупках Казахстана</p></div>`;
    let d;
    try { d = await api("/auth/start", { method: "POST" }); }
    catch (e) { document.getElementById("login-box").innerHTML = `<p class="desc">${esc(e.message)}</p>`; return; }
    const box = document.getElementById("login-box");
    box.innerHTML = `
      <div class="code" aria-label="Код входа">${d.code.split("").map((c) => `<span>${c}</span>`).join("")}</div>
      <a class="btn btn-primary btn-block" href="${esc(d.link)}" target="_blank" rel="noopener">Подтвердить в Telegram</a>
      <ol class="login-steps"><li>Нажмите кнопку — откроется бот @${esc(d.bot)}</li><li>Нажмите «Запустить» или отправьте боту этот код</li><li>Страница откроется автоматически</li></ol>
      <p class="hint" id="login-timer"></p>`;
    const until = Date.now() + d.expires_in * 1000;
    pollTimer = setInterval(async () => {
      const sec = Math.round((until - Date.now()) / 1000);
      const t = document.getElementById("login-timer");
      if (!t) return clearInterval(pollTimer);
      if (sec <= 0) { clearInterval(pollTimer); return renderLogin("Код устарел — вот новый."); }
      t.textContent = `Код действует ${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
      if (sec % 2) return;
      try {
        const r = await api("/auth/poll?nonce=" + encodeURIComponent(d.nonce));
        if (r.token) {
          clearInterval(pollTimer); setToken(r.token); document.body.classList.remove("auth");
          state.me = null; location.hash = "#feed"; route();
        }
      } catch (e) { if (e.status === 410) { clearInterval(pollTimer); renderLogin("Код устарел — вот новый."); } }
    }, 1000);
  }

  // ── Заявки (воронка + сроки) ────────────────────
  async function renderPipeline() {
    view.innerHTML = `<div class="loading"><span class="spinner"></span>Загрузка…</div>`;
    const [{ columns }, dl] = await Promise.all([api("/pipeline"), api("/deadlines")]);
    const total = Object.values(columns).reduce((a, c) => a + c.length, 0);
    const soon = dl.lots.slice(0, 6);
    const deadlinesHtml = soon.length || dl.documents.length ? `<section class="panel deadlines"><div class="panel-head"><h2>Ближайшие сроки</h2><span class="hint">Напоминания приходят в Telegram за 3 дня и за сутки</span></div>
      <div class="dl-list">${soon.map((x) => { const left = timeLeft(x.hours_left); const d = new Date(x.deadline);
        return `<a class="dl" href="#lot/${x.id}"><span class="dl-date"><b>${d.toLocaleString("ru-RU", { day: "numeric", timeZone: TZ })}</b>${d.toLocaleString("ru-RU", { month: "short", timeZone: TZ })}</span>
          <span class="dl-body"><span class="cell-title">${esc(x.title)}</span><span class="cell-sub">${esc(STAGE_NAME[x.stage])} · ${esc(moneyShort(x.amount))}</span></span><span class="left ${left.cls}">${esc(left.text)}</span></a>`; }).join("")}
      ${dl.documents.map((x) => `<a class="dl" href="#documents"><span class="dl-date doc"><b>!</b>док.</span><span class="dl-body"><span class="cell-title">${esc(x.title)}</span>
          <span class="cell-sub">${x.status === "expired" ? "Просрочен" : "Истекает"} ${esc(dateShort(x.expires_at))}</span></span><span class="status ${x.status === "expired" ? "bad" : "warn"}">${x.status === "expired" ? "Просрочен" : "Обновить"}</span></a>`).join("")}</div></section>` : "";
    view.innerHTML = `
      <div class="page-head"><div><h1>Заявки</h1><p class="page-sub">${total
        ? `${total} ${plural(total, "закупка", "закупки", "закупок")} в работе. Статус меняется в карточке закупки.`
        : "Добавьте закупку в работу из её карточки — так вы будете вести её до результата."}</p></div></div>
      ${deadlinesHtml}
      <div class="board">${STAGES.map(([k, n]) => {
        const items = columns[k] || [];
        const sum = items.reduce((a, x) => a + (x.amount || 0), 0);
        return `<section class="col"><div class="col-head"><h3>${n}</h3><span>${items.length}</span></div>
          ${items.map((x) => { const left = timeLeft(x.hours_left); return `<a class="card" href="#lot/${x.id}"><div class="card-title">${esc(x.title)}</div>
            <div class="card-meta"><span class="amount">${esc(moneyShort(x.amount))}</span><span class="left ${left.cls}">${esc(left.text)}</span></div></a>`; }).join("") || `<div class="col-empty">Нет закупок</div>`}
          ${sum ? `<div class="col-sum">Итого ${esc(moneyShort(sum))}</div>` : ""}</section>`;
      }).join("")}</div>`;
  }

  // ── Профиль и онбординг ─────────────────────────
  function keywordEditor(initial) {
    const kws = [...initial];
    const box = document.createElement("div");
    box.className = "kw-box";
    const draw = () => {
      box.innerHTML = kws.map((k, i) => `<span class="kw">${esc(k)}<button type="button" data-i="${i}" aria-label="Удалить ${esc(k)}">×</button></span>`).join("")
        + `<input class="kw-input" placeholder="${kws.length ? "Добавить направление" : "Например: ремонт кровли"}" aria-label="Добавить направление">`;
      box.querySelectorAll("[data-i]").forEach((b) => b.addEventListener("click", () => { kws.splice(+b.dataset.i, 1); draw(); }));
      const inp = box.querySelector("input");
      inp.addEventListener("keydown", (e) => {
        if ((e.key === "Enter" || e.key === ",") && inp.value.trim()) {
          e.preventDefault();
          inp.value.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean).forEach((x) => { if (!kws.includes(x)) kws.push(x); });
          draw(); box.querySelector("input").focus();
        }
      });
    };
    draw();
    box.add = (k) => { k = k.trim().toLowerCase(); if (k && !kws.includes(k)) { kws.push(k); draw(); } };
    box.value = () => {
      const pending = box.querySelector("input").value.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);
      return [...kws, ...pending.filter((x) => !kws.includes(x))];
    };
    return box;
  }
  function amountSelect(min, max) {
    const i = Math.max(0, AMOUNTS.findIndex(([, a, b]) => a === min && b === max));
    const sel = document.createElement("select");
    sel.className = "select"; sel.id = "amount";
    sel.innerHTML = AMOUNTS.map(([n], k) => `<option value="${k}" ${k === i ? "selected" : ""}>${n}</option>`).join("");
    sel.value_ = () => ({ min_amount: AMOUNTS[+sel.value][1], max_amount: AMOUNTS[+sel.value][2] });
    return sel;
  }

  function renderOnboarding() {
    view.innerHTML = `<div class="onboard">
      <h1>Что поставляет ваша компания?</h1>
      <p class="lead">Это первый и единственный шаг настройки. Выберите направления или впишите свои — Kyran сразу покажет открытые закупки по ним.</p>
      <div class="form">
        <div class="form-row"><label class="title">Направления деятельности</label><p class="hint">Нажмите на подходящие или впишите своё и нажмите Enter.</p>
          <div class="ask-chips" id="kw-suggest">${["ремонт кровли", "ремонт школ", "благоустройство", "канцтовары", "бумага", "мебель", "компьютерная техника", "продукты питания", "уборка помещений", "спецодежда", "медицинские расходные материалы", "видеонаблюдение"].map((k) => `<button class="chip" type="button">${k}</button>`).join("")}</div>
          <div id="kw"></div></div>
        <div class="form-row"><label class="title" for="amount">Диапазон сумм</label><div id="am"></div></div>
        <div class="form-actions"><button class="btn btn-primary" id="go">Показать подходящие закупки</button></div>
      </div>
      <div class="steps">
        <div><strong>Подбор</strong>Закупки сопоставляются с профилем и получают оценку совпадения.</div>
        <div><strong>Анализ</strong>ИИ готовит заключение по требованиям, документам и рискам.</div>
        <div><strong>Контроль сроков</strong>Уведомления о закупках с близким сроком подачи.</div>
      </div></div>`;
    const kw = keywordEditor([]); view.querySelector("#kw").append(kw);
    view.querySelectorAll("#kw-suggest .chip").forEach((c) => c.addEventListener("click", () => { kw.add(c.textContent); c.setAttribute("aria-pressed", "true"); }));
    const am = amountSelect(null, null); view.querySelector("#am").append(am);
    view.querySelector("#go").addEventListener("click", async (e) => {
      const keywords = kw.value();
      if (!keywords.length) return toast("Укажите хотя бы одно направление");
      e.target.disabled = true; e.target.textContent = "Подбор…";
      await api("/me", { method: "PUT", body: { keywords, ...am.value_() } });
      await loadMe(); route();
    });
  }

  const PROFILE_TABS = [["company", "Компания"], ["matching", "Подбор закупок"], ["notify", "Уведомления"], ["plan", "Тариф"], ["security", "Вход и безопасность"]];

  function renderProfile(tab) {
    tab = PROFILE_TABS.some(([k]) => k === tab) ? tab : "company";
    const u = state.me.user;
    view.innerHTML = `
      <div class="profile-head">
        <span class="avatar xl">${esc(initials(u))}</span>
        <div><h1>${esc(u.company_name || u.name || "Моя компания")}</h1>
          <p class="page-sub">${u.company_bin ? "БИН " + esc(u.company_bin) + " · " : ""}${u.username ? "@" + esc(u.username) : "Telegram ID " + esc(u.id)}</p></div>
      </div>
      <div class="subnav" role="tablist">${PROFILE_TABS.map(([k, n]) => `<a href="#profile/${k}" role="tab" aria-selected="${k === tab}">${n}</a>`).join("")}</div>
      <div id="ptab"></div>`;
    const box = view.querySelector("#ptab");
    ({ company: profCompany, matching: profMatching, notify: profNotify, plan: profPlan, security: profSecurity })[tab](box, u);
  }

  async function saveMe(btn, body, msg) {
    btn.disabled = true;
    try { await api("/me", { method: "PUT", body }); await loadMe(); haptic(); toast(msg || "Сохранено"); }
    catch (err) { toast(err.message); }
    btn.disabled = false;
  }

  function profCompany(box, u) {
    box.innerHTML = `<section class="panel"><div class="panel-body"><div class="form">
      <div class="form-grid">
        <label class="form-row"><span class="title">Название компании</span><input id="cname" class="input" maxlength="300" value="${esc(u.company_name)}" placeholder="ТОО «Название»"></label>
        <label class="form-row"><span class="title">БИН / ИИН</span><input id="bin" class="input" inputmode="numeric" maxlength="12" value="${esc(u.company_bin)}" placeholder="12 цифр"></label>
      </div>
      <label class="form-row"><span class="title">О компании</span><span class="hint">Опыт, лицензии, техника, штат, регионы работы. ИИ учитывает это при оценке закупок, в разборах и в техническом предложении.</span>
        <textarea id="about" class="textarea" rows="6" maxlength="2000">${esc(u.about)}</textarea></label>
      <div class="form-section"><h3>Реквизиты для документов заявки</h3><p class="hint">Подставляются в письма, опись и техническое предложение.</p></div>
      <div class="form-grid">
        <label class="form-row"><span class="title">Руководитель (ФИО)</span><input class="input" data-r="director" maxlength="200" value="${esc(u.director)}" placeholder="Ахметов Ерлан Серикович"></label>
        <label class="form-row"><span class="title">Должность</span><input class="input" data-r="director_title" maxlength="100" value="${esc(u.director_title || "Директор")}"></label>
        <label class="form-row full"><span class="title">Юридический адрес</span><input class="input" data-r="address" maxlength="400" value="${esc(u.address)}" placeholder="г. Астана, ул. Кенесары 40, офис 12"></label>
        <label class="form-row"><span class="title">Телефон</span><input class="input" data-r="phone" maxlength="60" inputmode="tel" value="${esc(u.phone)}" placeholder="+7 7__ ___ __ __"></label>
        <label class="form-row"><span class="title">E-mail</span><input class="input" data-r="email" maxlength="120" inputmode="email" value="${esc(u.email)}"></label>
        <label class="form-row"><span class="title">Банк</span><input class="input" data-r="bank_name" maxlength="200" value="${esc(u.bank_name)}" placeholder="АО «Kaspi Bank»"></label>
        <label class="form-row"><span class="title">БИК</span><input class="input" data-r="bik" maxlength="20" value="${esc(u.bik)}"></label>
        <label class="form-row full"><span class="title">IBAN</span><input class="input" data-r="iban" maxlength="40" value="${esc(u.iban)}" placeholder="KZ__ ____ ____ ____ ____"></label>
      </div>
      <div class="form-actions"><button class="btn btn-primary" id="save">Сохранить</button></div>
    </div></div></section>`;
    box.querySelector("#save").addEventListener("click", (e) => {
      const bin = box.querySelector("#bin").value.replace(/\D/g, "");
      if (bin && bin.length !== 12) return toast("БИН должен содержать 12 цифр");
      const req = {};
      box.querySelectorAll("[data-r]").forEach((i) => (req[i.dataset.r] = i.value.trim()));
      saveMe(e.target, { company_bin: bin, company_name: box.querySelector("#cname").value.trim(), about: box.querySelector("#about").value, ...req });
    });
  }

  function profMatching(box, u) {
    box.innerHTML = `<section class="panel"><div class="panel-body"><div class="form">
      <div class="form-row"><span class="title">Направления деятельности</span><span class="hint">Товары и работы, которые поставляет компания. Каждое направление — отдельной фразой.</span><div id="kw"></div></div>
      <div class="form-row"><span class="title">Диапазон сумм</span><div id="am"></div></div>
      <div class="form-actions"><button class="btn btn-primary" id="save">Сохранить и обновить подбор</button></div>
    </div></div></section>`;
    const kw = keywordEditor(u.keywords); box.querySelector("#kw").append(kw);
    const am = amountSelect(u.min_amount, u.max_amount); box.querySelector("#am").append(am);
    box.querySelector("#save").addEventListener("click", (e) => saveMe(e.target, { keywords: kw.value(), ...am.value_() }, "Подбор обновлён"));
  }

  function profNotify(box, u) {
    const bot = state.me.bot;
    box.innerHTML = `<section class="panel"><div class="panel-body"><div class="form">
      <label class="toggle"><input type="checkbox" id="notify" ${u.notify ? "checked" : ""}><span>Присылать новые подходящие закупки в Telegram</span></label>
      <div class="info-list">
        <div><strong>Срочные закупки</strong><span>Сразу, когда появляется закупка со сроком подачи меньше 3 дней</span></div>
        <div><strong>Ежедневная подборка</strong><span>Каждое утро в 9:00 — лучшие новые закупки</span></div>
        <div><strong>Сроки по заявкам</strong><span>За 3 дня и за сутки до окончания приёма заявок по закупкам в работе</span></div>
        <div><strong>Документы</strong><span>За 30 и 7 дней до окончания срока действия</span></div>
        <div><strong>Сайты заказчиков</strong><span>Мониторинг страниц закупок без API — команды /watch, /sites и /unwatch в боте</span></div>
      </div>
      <div class="form-actions"><button class="btn btn-primary" id="save">Сохранить</button>${bot ? `<a class="btn" href="https://t.me/${esc(bot)}" target="_blank" rel="noopener">Открыть бота</a>` : ""}</div>
    </div></div></section>`;
    box.querySelector("#save").addEventListener("click", (e) => saveMe(e.target, { notify: box.querySelector("#notify").checked }));
  }

  function profPlan(box) {
    const p = state.me.plan;
    const bar = (used, lim) => `<span class="match-bar wide"><i style="--p:${Math.min(100, Math.round((used / lim) * 100))}"></i></span>`;
    box.innerHTML = `
      <section class="panel"><div class="panel-body plan-now">
        <div><span class="field-label">Текущий тариф</span><div class="plan-name">${esc(p.name)}</div>
          <div class="cell-sub">${p.until ? (p.active ? "Действует до " : "Закончился ") + esc(dateShort(p.until)) : ""}</div></div>
        <div class="usage"><div><span>ИИ-разборы в этом месяце</span><b>${p.usage.analyses} / ${p.limits.analyses}</b></div>${bar(p.usage.analyses, p.limits.analyses)}</div>
        <div class="usage"><div><span>Документы заявки от ИИ</span><b>${p.usage.generations} / ${p.limits.generations}</b></div>${bar(p.usage.generations, p.limits.generations)}</div>
        <div class="usage"><div><span>Документы в библиотеке</span><b>${p.usage.documents} / ${p.limits.documents}</b></div>${bar(p.usage.documents, p.limits.documents)}</div>
      </div></section>
      <div class="plans">${p.catalog.map((c) => `<section class="plan ${c.code === "pro" ? "featured" : ""} ${c.code === p.code && p.active ? "current" : ""}">
        ${c.code === "pro" ? `<span class="plan-flag">Выбор большинства</span>` : ""}
        <h3>${esc(c.name)}</h3><div class="price">${c.price.toLocaleString("ru-RU")} ₸<small>/мес</small></div>
        <ul>${c.features.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
        ${c.code === p.code && p.active ? `<button class="btn btn-block" data-plan="${c.code}">Продлить</button>` : `<button class="btn ${c.code === "pro" ? "btn-primary" : ""} btn-block" data-plan="${c.code}">Подключить</button>`}
      </section>`).join("")}</div>
      <p class="hint" style="margin-top:12px">Оплата по счёту для ТОО и ИП или через Kaspi. При оплате за 12 месяцев — 2 месяца в подарок.</p>
      <section class="panel" id="req" hidden></section>`;
    box.querySelectorAll("[data-plan]").forEach((b) => b.addEventListener("click", () => {
      const c = p.catalog.find((x) => x.code === b.dataset.plan);
      const req = box.querySelector("#req"); req.hidden = false;
      req.innerHTML = `<div class="panel-head"><h2>Подключение тарифа «${esc(c.name)}»</h2></div><form class="panel-body form">
        <div class="form-grid">
          <label class="form-row"><span class="title">Срок</span><select class="select" name="months"><option value="1">1 месяц — ${c.price.toLocaleString("ru-RU")} ₸</option>
            <option value="3">3 месяца — ${(c.price * 3).toLocaleString("ru-RU")} ₸</option><option value="12">12 месяцев — ${(c.price * 10).toLocaleString("ru-RU")} ₸</option></select></label>
          <label class="form-row"><span class="title">Телефон или e-mail для счёта</span><input class="input" name="contact" maxlength="200" placeholder="+7 7__ ___ __ __"></label>
        </div>
        <div class="form-actions"><button class="btn btn-primary" type="submit">Отправить заявку</button></div></form>`;
      req.querySelector("form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const fd = new FormData(e.target);
        try {
          const r = await api("/billing/request", { method: "POST", body: { plan: c.code, months: +fd.get("months"), contact: fd.get("contact") } });
          req.innerHTML = `<div class="panel-body"><h3 style="margin:0 0 6px">Заявка №${r.id} принята</h3><p class="desc">${esc(r.payment_info)}</p></div>`;
        } catch (err) { toast(err.message); }
      });
      req.scrollIntoView({ behavior: "smooth", block: "start" });
    }));
  }

  function profSecurity(box, u) {
    box.innerHTML = `<section class="panel"><div class="panel-body"><div class="form">
      <div class="info-list">
        <div><strong>Способ входа</strong><span>Одноразовый код, подтверждаемый в Telegram. Пароль не хранится — его невозможно украсть или подобрать.</span></div>
        <div><strong>Аккаунт Telegram</strong><span>${u.username ? "@" + esc(u.username) : "ID " + esc(u.id)}</span></div>
        <div><strong>Срок сессии</strong><span>30 дней на каждом устройстве</span></div>
      </div>
      <div class="form-actions">
        ${tg ? "" : `<button class="btn" id="logout">Выйти</button>`}
        <button class="btn btn-danger" id="logout-all">Выйти на всех устройствах</button></div>
    </div></div></section>`;
    const lo = box.querySelector("#logout");
    if (lo) lo.addEventListener("click", () => { setToken(null); state.me = null; renderLogin("Вы вышли из аккаунта."); });
    box.querySelector("#logout-all").addEventListener("click", async () => {
      if (!confirm("Завершить все сессии на сайте, включая эту?")) return;
      await api("/auth/logout-all", { method: "POST" });
      setToken(null); state.me = null;
      if (tg) toast("Сессии в браузерах завершены"); else renderLogin("Все сессии завершены.");
    });
  }

  function noAccess(message) {
    view.innerHTML = `<div class="onboard"><h1>Вход в Kyran</h1>
      <p class="lead">${esc(message || "Откройте Kyran через Telegram-бота. Для работы с компьютера отправьте боту команду /web — придёт персональная ссылка.")}</p></div>`;
  }

  // ── Роутер ──────────────────────────────────────
  function goBack() { if (history.length > 1) history.back(); else location.hash = "#feed"; }

  async function route() {
    const [page, arg] = (location.hash.slice(1) || "feed").split("/");
    if (tg && page !== "lot" && page !== "rival") { tg.BackButton.hide(); tg.BackButton.offClick(goBack); }
    const tab = page === "lot" || page === "report" ? "feed" : page === "rival" ? "rivals" : page;
    document.body.classList.remove("auth"); clearInterval(pollTimer);
    document.querySelectorAll("[data-tab]").forEach((a) => (a.dataset.tab === tab ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
    window.scrollTo(0, 0);
    try {
      if (!state.me) await loadMe();
      if (page === "lot" && arg) return await renderLot(arg);
      if (page === "pipeline") return await renderPipeline();
      if (page === "documents") return await renderDocuments();
      if (page === "analyses") return await renderAnalyses();
      if (page === "report") return await renderReport();
      if (page === "rivals") return await renderRivals();
      if (page === "rival" && arg) return await renderRival(arg);
      if (page === "profile") return renderProfile(arg);
      return await renderFeed();
    } catch (e) {
      if (e.status === 401) { if (tg) return noAccess(e.message); setToken(null); state.me = null; return renderLogin(); }
      view.innerHTML = `<div class="empty"><h2>Не удалось загрузить данные</h2><p>${esc(e.message)}</p><p style="margin-top:12px"><button class="btn" onclick="location.reload()">Обновить</button></p></div>`;
    }
  }

  window.addEventListener("hashchange", route);
  if (!authHeader()) renderLogin(); else route();
})();
