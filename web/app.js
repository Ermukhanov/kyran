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
    const res = await fetch("/api" + path, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json", Authorization: authHeader() },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!res.ok) {
      let msg = "Ошибка " + res.status;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      const err = new Error(msg); err.status = res.status; throw err;
    }
    return res.json();
  }

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

  // ── Статус в боковой панели ─────────────────────
  function paintStatus() {
    const f = state.me.features;
    const real = Object.entries(f.sources).filter(([k, v]) => v && k !== "demo").length;
    document.getElementById("src-dot").className = "dot " + (real ? "on" : f.demo ? "demo" : "");
    document.getElementById("src-text").textContent = real ? "Портал госзакупок подключён" : f.demo ? "Демонстрационные данные" : "Источник не подключён";
    document.getElementById("ai-dot").className = "dot " + (f.ai ? "on" : "");
    document.getElementById("ai-text").textContent = f.ai ? "ИИ-модуль активен" : "ИИ-модуль не подключён";
    document.getElementById("demo-strip").hidden = !f.demo;
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
    view.innerHTML = `<div class="loading"><span class="spinner"></span>Загрузка…</div>`;
    if (tg) { tg.BackButton.show(); tg.BackButton.onClick(goBack); }
    const data = await api("/lots/" + id);
    const l = data.lot;
    const left = timeLeft(l.hours_left);
    const leftPct = l.hours_left == null ? 100 : Math.max(2, Math.min(100, (l.hours_left / (14 * 24)) * 100));
    const docs = (l.documents || []).filter((d) => d.url);
    view.innerHTML = `
      <div class="crumbs"><a href="#feed">Тендеры</a> / Закупка</div>
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
                <dt>Источник</dt><dd>${l.url ? `<a href="${esc(l.url)}" target="_blank" rel="noopener">Открыть на портале</a>` : esc(l.source === "demo" ? "Демонстрационные данные" : l.source)}</dd>
              </dl>
              ${l.reason ? `<p class="reason">Почему подходит: ${esc(l.reason)}</p>` : ""}
            </div>
          </section>

          <section class="panel" id="analysis-panel">
            <div class="panel-head"><h2>ИИ-разбор</h2><span id="analysis-action"></span></div>
            <div id="analysis"></div>
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
        </aside>
      </div>`;

    renderAnalysis(id, data.analysis, data.ai);
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
      haptic(); renderAnalysis(id, analysis, true);
    } catch (e) {
      box.innerHTML = `<div class="panel-body"><p class="desc">${esc(e.message)}</p></div>`;
      document.getElementById("analysis-action").innerHTML = `<button class="btn">Повторить</button>`;
      document.querySelector("#analysis-action button").addEventListener("click", () => runAnalysis(id, refresh));
    }
  }

  // ── Воронка ─────────────────────────────────────
  async function renderPipeline() {
    view.innerHTML = `<div class="loading"><span class="spinner"></span>Загрузка…</div>`;
    const { columns } = await api("/pipeline");
    const total = Object.values(columns).reduce((a, c) => a + c.length, 0);
    view.innerHTML = `
      <div class="page-head"><div><h1>Воронка</h1><p class="page-sub">${total
        ? `${total} ${plural(total, "закупка", "закупки", "закупок")} в работе. Статус меняется в карточке закупки.`
        : "Добавьте закупку в воронку из её карточки — так вы будете вести её до результата."}</p></div></div>
      <div class="board">${STAGES.map(([k, n]) => {
        const items = columns[k] || [];
        const sum = items.reduce((a, x) => a + (x.amount || 0), 0);
        return `<section class="col"><div class="col-head"><h3>${n}</h3><span>${items.length}</span></div>
          ${items.map((x) => { const left = timeLeft(x.hours_left); return `<a class="card" href="#lot/${x.id}"><div class="card-title">${esc(x.title)}</div>
            <div class="card-meta"><span class="amount">${esc(moneyShort(x.amount))}</span><span class="left ${left.cls}">${esc(left.text)}</span></div></a>`; }).join("") || `<div class="col-empty">Нет закупок</div>`}
          ${sum ? `<div class="col-sum">Итого ${esc(moneyShort(sum))}</div>` : ""}</section>`;
      }).join("")}</div>`;
  }

  // ── Мониторинг сайтов ───────────────────────────
  async function renderSites() {
    view.innerHTML = `<div class="loading"><span class="spinner"></span>Загрузка…</div>`;
    const { items, ai } = await api("/monitors");
    view.innerHTML = `
      <div class="page-head"><div><h1>Мониторинг сайтов</h1>
        <p class="page-sub">Отслеживание закупок на площадках без API: сайты заказчиков, корпоративные порталы, разделы «Тендеры». ${ai
          ? "Новые закупки извлекаются автоматически." : "При изменении страницы придёт уведомление."}</p></div></div>
      <form class="toolbar" id="site-form">
        <input class="input" style="flex:1;min-width:240px" name="url" type="url" inputmode="url" placeholder="https://example.kz/tenders" required aria-label="Адрес страницы">
        <button class="btn btn-primary" type="submit">Добавить</button>
      </form>
      <section class="panel sites">${items.length ? items.map((m) => `
        <div class="site"><div><div class="site-title">${esc(m.title)}</div><div class="site-url">${esc(m.url)}</div>
          <div class="site-status ${m.error ? "err" : ""}">${m.error ? "Ошибка доступа: " + esc(m.error)
            : m.last_change ? "Последнее изменение: " + esc(dateTime(m.last_change)) : "Изменений не обнаружено"}</div></div>
          <button class="btn" data-del="${m.id}">Удалить</button></div>`).join("")
        : `<div class="empty"><h2>Нет отслеживаемых страниц</h2><p>Добавьте адрес страницы с закупками — Kyran будет проверять её несколько раз в день.</p></div>`}</section>`;
    view.querySelector("#site-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = e.target.querySelector("button");
      btn.disabled = true; btn.textContent = "Проверка…";
      try { await api("/monitors", { method: "POST", body: { url: e.target.url.value } }); toast("Страница добавлена"); renderSites(); }
      catch (err) { toast(err.message); btn.disabled = false; btn.textContent = "Добавить"; }
    });
    view.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      await api("/monitors/" + b.dataset.del, { method: "DELETE" }); toast("Страница удалена"); renderSites();
    }));
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
      <h1>Настройте подбор закупок</h1>
      <p class="lead">Укажите направления деятельности компании. Kyran будет отбирать открытые закупки по этим направлениям и присылать срочные в Telegram.</p>
      <div class="form">
        <div class="form-row"><label class="title">Направления деятельности</label><p class="hint">Товары или работы, которые вы поставляете. Через запятую или Enter.</p><div id="kw"></div></div>
        <div class="form-row"><label class="title" for="amount">Диапазон сумм</label><div id="am"></div></div>
        <div class="form-actions"><button class="btn btn-primary" id="go">Показать подходящие закупки</button></div>
      </div>
      <div class="steps">
        <div><strong>Подбор</strong>Закупки сопоставляются с профилем и получают оценку совпадения.</div>
        <div><strong>Анализ</strong>ИИ готовит заключение по требованиям, документам и рискам.</div>
        <div><strong>Контроль сроков</strong>Уведомления о закупках с близким сроком подачи.</div>
      </div></div>`;
    const kw = keywordEditor([]); view.querySelector("#kw").append(kw);
    const am = amountSelect(null, null); view.querySelector("#am").append(am);
    view.querySelector("#go").addEventListener("click", async (e) => {
      const keywords = kw.value();
      if (!keywords.length) return toast("Укажите хотя бы одно направление");
      e.target.disabled = true; e.target.textContent = "Подбор…";
      await api("/me", { method: "PUT", body: { keywords, ...am.value_() } });
      await loadMe(); route();
    });
  }

  function renderProfile() {
    const u = state.me.user;
    view.innerHTML = `
      <div class="page-head"><div><h1>Профиль компании</h1><p class="page-sub">По этим данным Kyran подбирает и оценивает закупки.</p></div></div>
      <section class="panel"><div class="panel-body"><div class="form">
        <div class="form-row"><label class="title">Направления деятельности</label><p class="hint">Товары и работы, которые поставляет компания</p><div id="kw"></div></div>
        <div class="form-row"><label class="title" for="amount">Диапазон сумм</label><div id="am"></div></div>
        <div class="form-row"><label class="title" for="about">О компании</label><p class="hint">Опыт, лицензии, техника, регионы работы. Учитывается ИИ при оценке.</p>
          <textarea id="about" class="textarea" maxlength="2000">${esc(u.about)}</textarea></div>
        <div class="form-row"><label class="title" for="bin">БИН</label><input id="bin" class="input" style="max-width:220px" inputmode="numeric" maxlength="12" value="${esc(u.company_bin)}" placeholder="12 цифр"></div>
        <label class="toggle"><input type="checkbox" id="notify" ${u.notify ? "checked" : ""}><span>Уведомления в Telegram</span></label>
        <div class="form-actions"><button class="btn btn-primary" id="save">Сохранить</button></div>
      </div></div></section>
      <p class="page-sub" style="margin-top:16px">Доступ с компьютера: отправьте боту Kyran команду /web — вы получите персональную ссылку.</p>`;
    const kw = keywordEditor(u.keywords); view.querySelector("#kw").append(kw);
    const am = amountSelect(u.min_amount, u.max_amount); view.querySelector("#am").append(am);
    view.querySelector("#save").addEventListener("click", async (e) => {
      const bin = view.querySelector("#bin").value.replace(/\D/g, "");
      if (bin && bin.length !== 12) return toast("БИН должен содержать 12 цифр");
      e.target.disabled = true;
      try {
        await api("/me", { method: "PUT", body: { keywords: kw.value(), ...am.value_(), about: view.querySelector("#about").value,
          company_bin: bin, notify: view.querySelector("#notify").checked } });
        await loadMe(); haptic(); toast("Профиль сохранён, подбор обновлён");
      } catch (err) { toast(err.message); }
      e.target.disabled = false;
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
    if (tg && page !== "lot") { tg.BackButton.hide(); tg.BackButton.offClick(goBack); }
    const tab = page === "lot" ? "feed" : page;
    document.querySelectorAll("[data-tab]").forEach((a) => (a.dataset.tab === tab ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
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
      view.innerHTML = `<div class="empty"><h2>Не удалось загрузить данные</h2><p>${esc(e.message)}</p><p style="margin-top:12px"><button class="btn" onclick="location.reload()">Обновить</button></p></div>`;
    }
  }

  window.addEventListener("hashchange", route);
  if (!authHeader()) noAccess(); else route();
})();
