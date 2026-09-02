(() => {
  const D = window.WS_DATA;
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const monthSelect = $("#month-select");
  const dateSelect = $("#date-select");
  const stockSearch = $("#stock-search");
  const HISTORY_PAGE_SIZE = 10;

  const state = {
    activeTab: "overview",
    mainSide: "BUY",
    observationMode: "current",
    historyPage: 1,
    selectedStockKey: "",
  };

  const esc = value => String(value ?? "—").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  }[char]));
  const clean = value => value === null || value === undefined || value === "" || value === "—" ? "—" : value;
  const number = value => typeof value === "number" ? value.toLocaleString("zh-TW") : clean(value);
  const textMatch = (query, ...values) => values.some(value => String(value || "").toLowerCase().includes(query));
  const isNumeric = value => typeof value === "number" && Number.isFinite(value);
  const formatReturn = value => isNumeric(value) ? `${value > 0 ? "+" : ""}${value.toFixed(1)}%` : "—";
  const outcome = value => !isNumeric(value) ? "active" : value > 0 ? "win" : value < 0 ? "loss" : "flat";

  function sideBadge(side) {
    const normalized = String(side || "").toUpperCase();
    if (normalized.includes("SELL")) return `<span class="badge sell">${esc(side)}</span>`;
    if (normalized.includes("BUY")) return `<span class="badge buy">${esc(side)}</span>`;
    if (normalized.includes("EXCHANGE")) return `<span class="badge warn">${esc(side)}</span>`;
    return `<span class="badge neutral">${esc(side || "Unknown")}</span>`;
  }

  function dateRows(date) {
    return {
      raw: D.raw.filter(row => row.Date === date),
      buy: D.mainforce.filter(row => row["日期"] === date && row["方向"] === "BUY"),
      sell: D.mainforce.filter(row => row["日期"] === date && row["方向"] === "SELL"),
      obs: D.observationSnapshots.filter(row => row["日期"] === date),
    };
  }

  function selectTab(tab, {focus = false, scroll = true} = {}) {
    state.activeTab = tab;
    $$(".tab-button").forEach(button => {
      const selected = button.dataset.tab === tab;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      if (selected && focus) button.focus();
    });
    $$(".tab-panel").forEach(panel => {
      const selected = panel.dataset.panel === tab;
      panel.classList.toggle("active", selected);
      panel.hidden = !selected;
    });
    window.history.replaceState(null, "", `#${tab}`);
    if (scroll) window.scrollTo({top: 0, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"});
  }

  function setupNavigation() {
    const buttons = $$(".tab-button");
    buttons.forEach((button, index) => {
      button.addEventListener("click", () => selectTab(button.dataset.tab));
      button.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        let nextIndex = index;
        if (event.key === "ArrowRight") nextIndex = (index + 1) % buttons.length;
        if (event.key === "ArrowLeft") nextIndex = (index - 1 + buttons.length) % buttons.length;
        if (event.key === "Home") nextIndex = 0;
        if (event.key === "End") nextIndex = buttons.length - 1;
        selectTab(buttons[nextIndex].dataset.tab, {focus: true});
      });
    });
    $$("[data-go-tab]").forEach(button => button.addEventListener("click", () => selectTab(button.dataset.goTab)));
    const requested = location.hash.slice(1);
    const initial = buttons.some(button => button.dataset.tab === requested) ? requested : "overview";
    selectTab(initial, {scroll: false});
    window.addEventListener("hashchange", () => {
      const tab = location.hash.slice(1);
      if (buttons.some(button => button.dataset.tab === tab)) selectTab(tab, {scroll: false});
    });
  }

  function renderPills(dates, current) {
    $("#date-pills").innerHTML = dates.map(date => `
      <button class="date-pill ${date === current ? "active" : ""}" type="button" data-date="${date}" aria-pressed="${date === current}">${date.slice(5)}</button>
    `).join("");
    $$(".date-pill").forEach(button => button.addEventListener("click", () => {
      dateSelect.value = button.dataset.date;
      dateSelect.dataset.prev = button.dataset.date;
      renderPills(dates, button.dataset.date);
      render();
    }));
  }

  function setMonth(month, keepDate = false) {
    monthSelect.value = month;
    const dates = D.meta.dates.filter(date => date.startsWith(month));
    const previous = dateSelect.dataset.prev;
    dateSelect.innerHTML = dates.map(date => `<option value="${date}">${date}</option>`).join("");
    const current = keepDate && dates.includes(previous) ? previous : dates[dates.length - 1];
    if (current) {
      dateSelect.value = current;
      dateSelect.dataset.prev = current;
    }
    renderPills(dates, current);
    render();
  }

  function renderKpis({raw, buy, sell, obs}) {
    const rawStocks = new Set(raw.map(row => row.Underlying_Code));
    const items = [
      ["Raw 權證", raw.length, "張"],
      ["Raw 母股", rawStocks.size, "檔"],
      ["觀察快照", obs.length, "檔"],
      ["買方榜", buy.length, "筆"],
      ["賣方榜", sell.length, "筆"],
    ];
    $("#kpis").innerHTML = items.map(([label, value, unit]) => `
      <article class="kpi"><span>${label}</span><div><strong>${value}</strong><small>${unit}</small></div></article>
    `).join("");
  }

  function renderOverview(rows, date) {
    $("#overview-date").textContent = date;
    const rawGroups = new Map();
    rows.raw.forEach(row => {
      const key = `${row.Underlying_Code}|${row.Underlying_Name}`;
      rawGroups.set(key, (rawGroups.get(key) || 0) + 1);
    });
    const topRaw = [...rawGroups.entries()].sort((a, b) => b[1] - a[1])[0];
    const maxRaw = rows.raw.filter(row => isNumeric(row.Displayed_Multiple)).sort((a, b) => b.Displayed_Multiple - a.Displayed_Multiple)[0];
    const topBuy = rows.buy.slice().sort((a, b) => Number(a["排名"] || 999) - Number(b["排名"] || 999))[0];
    const topSell = rows.sell.slice().sort((a, b) => Number(a["排名"] || 999) - Number(b["排名"] || 999))[0];
    const [topCode, topName] = topRaw ? topRaw[0].split("|") : ["—", "沒有 Raw"];
    const focus = [
      ["Raw 最集中", topRaw ? `${topCode} ${topName}` : "沒有 Raw", topRaw ? `${topRaw[1]} 張權證同時觸發` : "所選日期無 Raw Trigger", "raw"],
      ["最高倍數", maxRaw ? `${maxRaw.Displayed_Multiple}x` : "—", maxRaw ? `${maxRaw.Underlying_Code} ${maxRaw.Underlying_Name} · ${maxRaw.Warrant_Code}` : "尚無可計算資料", "raw"],
      ["買方焦點", topBuy ? `${topBuy["母股代號"]} ${topBuy["母股名稱"]}` : "尚未收錄", topBuy ? `BUY #${topBuy["排名"]}` : "所選日期無買方排行", "mainforce"],
      ["賣方焦點", topSell ? `${topSell["母股代號"]} ${topSell["母股名稱"]}` : "尚未收錄", topSell ? `SELL #${topSell["排名"]}` : "所選日期無賣方排行", "mainforce"],
    ];
    $("#overview-focus").innerHTML = focus.map(([label, value, meta, tab]) => `
      <button type="button" class="focus-item" data-go-tab="${tab}">
        <span>${label}</span><strong>${esc(value)}</strong><small>${esc(meta)}</small>
      </button>
    `).join("");
    $$("#overview-focus [data-go-tab]").forEach(button => button.addEventListener("click", () => selectTab(button.dataset.goTab)));
  }

  function groupRaw(raw) {
    const groups = {};
    raw.forEach(row => {
      const key = `${row.Underlying_Code}|${row.Underlying_Name}`;
      (groups[key] ||= []).push(row);
    });
    return groups;
  }

  function renderRaw(raw) {
    const groups = groupRaw(raw);
    $("#raw-summary").textContent = `${raw.length} 張 / ${Object.keys(groups).length} 檔母股`;
    $("#underlying-cards").innerHTML = Object.entries(groups).map(([key, rows]) => {
      const [code, name] = key.split("|");
      const issuers = [...new Set(rows.map(row => row.Issuer).filter(value => value && value !== "—"))];
      const directions = [...new Set(rows.map(row => row.Trade_Direction).filter(value => value && value !== "Unknown"))];
      return `<button type="button" class="under-card ${rows.length >= 3 ? "hot" : ""}" data-stock="${esc(code)}">
        <strong>${esc(code)} ${esc(name)}</strong>
        <span>${rows.length} 張 Raw · ${issuers.length} issuer${directions.length ? ` · ${directions.map(esc).join(" / ")}` : ""}</span>
      </button>`;
    }).join("") || `<div class="empty">這一天沒有 Raw 資料</div>`;

    $("#raw-list").innerHTML = raw.length ? raw.map(row => `
      <article class="raw-card">
        <div class="card-title-row">
          <div><span class="stock-code">${esc(row.Underlying_Code)}</span><h3>${esc(row.Underlying_Name)}</h3></div>
          ${sideBadge(row.Trade_Direction)}
        </div>
        <div class="warrant-line"><strong>${esc(row.Warrant_Code)}</strong><span>${esc(clean(row.Warrant_Name))}</span><span class="issuer">${esc(clean(row.Issuer))}</span></div>
        <dl class="metric-grid">
          <div><dt>30 分成交量</dt><dd>${number(row["30m_Volume"])}</dd></div>
          <div><dt>流通量</dt><dd>${number(row.Circulation)}</dd></div>
          <div><dt>倍數</dt><dd>${esc(clean(row.Displayed_Multiple))}${isNumeric(row.Displayed_Multiple) ? "x" : ""}</dd></div>
        </dl>
        <div class="context-block"><span>${esc(clean(row.Episode_Type))}</span><p>${esc(clean(row.Notes))}</p></div>
      </article>
    `).join("") : `<div class="empty">所選日期沒有 Raw Trigger</div>`;

    $("#raw-body").innerHTML = raw.map(row => `<tr><td>${esc(row.Underlying_Code)}</td><td>${esc(row.Warrant_Code)}</td></tr>`).join("");
    $$("[data-stock]").forEach(button => button.addEventListener("click", () => openStock(button.dataset.stock)));
  }

  function rankCompleteness(rows) {
    if (!rows.length) return "尚未收到";
    return [...new Set(rows.map(row => row["資料完整度"]).filter(Boolean))].join(" / ");
  }

  function renderRank(rows, target, badgeId, side) {
    $(badgeId).textContent = rankCompleteness(rows);
    $(badgeId).className = `badge ${side === "BUY" ? "buy" : "sell"}`;
    if (!rows.length) {
      $(target).innerHTML = `<div class="empty">這一天的${side === "BUY" ? "買方" : "賣方"}排行尚未收錄</div>`;
      return;
    }
    $(target).innerHTML = `<div class="rank-list">${rows.slice().sort((a, b) => Number(a["排名"] || 999) - Number(b["排名"] || 999)).map(row => `
      <button type="button" class="rank-row ${side === "BUY" ? "buy-row" : "sell-row"}" data-stock="${esc(row["母股代號"])}">
        <span class="rank-no">#${esc(clean(row["排名"]))}</span>
        <span class="rank-stock"><strong>${esc(clean(row["母股名稱"]))}</strong><small>${esc(clean(row["母股代號"]))}</small></span>
        <span class="rank-amt">${row["可見金額(萬)"] == null ? "—" : `${number(row["可見金額(萬)"])} 萬`}</span>
        ${row["當日Raw"] === true ? '<span class="raw-dot">RAW</span>' : '<span></span>'}
      </button>
    `).join("")}</div>`;
    $$(`${target} [data-stock]`).forEach(button => button.addEventListener("click", () => openStock(button.dataset.stock)));
  }

  function selectMainSide(side) {
    state.mainSide = side;
    $$("[data-side]").forEach(button => {
      const selected = button.dataset.side === side;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    $("#buy-panel").classList.toggle("hidden", side !== "BUY");
    $("#sell-panel").classList.toggle("hidden", side !== "SELL");
  }

  function observationRows(date) {
    if (state.observationMode === "snapshot") {
      return D.observationSnapshots.filter(row => row["日期"] === date).map(row => ({
        code: row["母股代號"], name: row["母股名稱"], entry: row["進觀察日期"], status: row["狀態"],
        raw: row["當日Raw張數"], episode: row["Episode類型"], source: row["確認程度"], note: row["備註"], completeness: row["完整度"],
      }));
    }
    const rawCount = D.raw.filter(row => row.Date === date).reduce((counts, row) => {
      counts[row.Underlying_Code] = (counts[row.Underlying_Code] || 0) + 1;
      return counts;
    }, {});
    return D.currentObservation.map(row => ({
      code: row["母股代號"], name: row["母股名稱"], entry: row["列入觀察日"], status: row["狀態"],
      raw: rawCount[row["母股代號"]] || 0, episode: row["Episode Age"], source: row["資料來源"], note: row["備註"], completeness: "Current",
    }));
  }

  function renderObservation(date) {
    const query = $("#observation-search").value.trim().toLowerCase();
    const sort = $("#observation-sort").value;
    let rows = observationRows(date).filter(row => !query || textMatch(query, row.code, row.name));
    rows.sort((a, b) => {
      if (sort === "oldest") return String(a.entry || "").localeCompare(String(b.entry || ""));
      if (sort === "raw") return Number(b.raw || 0) - Number(a.raw || 0) || String(b.entry || "").localeCompare(String(a.entry || ""));
      if (sort === "code") return String(a.code || "").localeCompare(String(b.code || ""), "zh-Hant", {numeric: true});
      return String(b.entry || "").localeCompare(String(a.entry || ""));
    });
    const modeLabel = state.observationMode === "current" ? "目前名單" : `${date} 快照`;
    $("#obs-summary").textContent = `${modeLabel} · ${rows.length} 檔`;
    $("#observation-list").innerHTML = rows.length ? rows.map(row => `
      <button type="button" class="obs-card" data-stock="${esc(row.code)}">
        <div class="obs-title"><span><b>${esc(clean(row.code))}</b> ${esc(clean(row.name))}</span><span class="badge neutral">${esc(clean(row.episode))}</span></div>
        <div class="obs-metrics"><span>進觀察 <b>${esc(clean(row.entry))}</b></span><span>選定日 Raw <b>${number(row.raw)} 張</b></span></div>
        <p>${esc(clean(row.note))}</p>
        <small>${esc(clean(row.source))}</small>
      </button>
    `).join("") : `<div class="empty">${state.observationMode === "snapshot" ? "所選日期沒有完整觀察快照；不會用後來的名單補成歷史資料。" : "沒有符合搜尋條件的目前觀察標的。"}</div>`;
    $$("#observation-list [data-stock]").forEach(button => button.addEventListener("click", () => openStock(button.dataset.stock)));
  }

  function renderHistoryKpis() {
    const completed = D.episodes.filter(row => isNumeric(row["歷史報酬%"]));
    const wins = completed.filter(row => row["歷史報酬%"] > 0);
    const average = completed.length ? completed.reduce((sum, row) => sum + row["歷史報酬%"], 0) / completed.length : null;
    const items = [
      ["全部 Episode", D.episodes.length, "筆"],
      ["已完成", completed.length, "筆"],
      ["勝率", completed.length ? `${Math.round(wins.length / completed.length * 100)}%` : "—", `${wins.length} 勝`],
      ["平均報酬", formatReturn(average), "已完成"],
    ];
    $("#history-kpis").innerHTML = items.map(([label, value, unit]) => `
      <article class="kpi"><span>${label}</span><div><strong>${value}</strong><small>${unit}</small></div></article>
    `).join("");
  }

  function historyRows() {
    const query = $("#history-search").value.trim().toLowerCase();
    const filter = $("#history-outcome").value;
    const sort = $("#history-sort").value;
    const rows = D.episodes.filter(row => {
      const matchesQuery = !query || textMatch(query, row["母股代號"], row["母股名稱"]);
      const matchesOutcome = filter === "all" || outcome(row["歷史報酬%"]) === filter;
      return matchesQuery && matchesOutcome;
    });
    rows.sort((a, b) => {
      if (sort === "exit-desc") return String(b["退出日"] || "").localeCompare(String(a["退出日"] || ""));
      if (sort.startsWith("return")) {
        const aValue = isNumeric(a["歷史報酬%"]) ? a["歷史報酬%"] : (sort === "return-desc" ? -Infinity : Infinity);
        const bValue = isNumeric(b["歷史報酬%"]) ? b["歷史報酬%"] : (sort === "return-desc" ? -Infinity : Infinity);
        return sort === "return-desc" ? bValue - aValue : aValue - bValue;
      }
      return String(b["進觀察日"] || "").localeCompare(String(a["進觀察日"] || ""));
    });
    return rows;
  }

  function renderHistory() {
    renderHistoryKpis();
    const rows = historyRows();
    const pageCount = Math.max(1, Math.ceil(rows.length / HISTORY_PAGE_SIZE));
    state.historyPage = Math.min(state.historyPage, pageCount);
    const start = (state.historyPage - 1) * HISTORY_PAGE_SIZE;
    const pageRows = rows.slice(start, start + HISTORY_PAGE_SIZE);
    $("#episode-list").innerHTML = pageRows.length ? pageRows.map(row => {
      const result = outcome(row["歷史報酬%"]);
      const resultLabel = result === "win" ? "勝" : result === "loss" ? "敗" : result === "flat" ? "持平" : "進行中";
      return `<button type="button" class="episode-card" data-stock="${esc(row["母股代號"])}">
        <div class="card-title-row">
          <div><span class="stock-code">${esc(clean(row["母股代號"]))}</span><h3>${esc(clean(row["母股名稱"]))}</h3></div>
          <span class="outcome ${result}">${resultLabel}</span>
        </div>
        <div class="episode-period"><span>進場 <b>${esc(clean(row["進觀察日"]))}</b></span><span>退出 <b>${esc(clean(row["退出日"]))}</b></span></div>
        <div class="return-line"><span>歷史報酬</span><strong class="${result}">${formatReturn(row["歷史報酬%"])}</strong></div>
        <p>${esc(clean(row["備註"]))}</p>
        <small>${esc(clean(row["資料用途"]))} · ${esc(clean(row["確認程度"]))}</small>
      </button>`;
    }).join("") : `<div class="empty">沒有符合搜尋或結果篩選的 Episode</div>`;

    const pages = Array.from({length: pageCount}, (_, index) => index + 1);
    $("#history-pagination").innerHTML = `
      <button type="button" data-page="${state.historyPage - 1}" ${state.historyPage === 1 ? "disabled" : ""} aria-label="上一頁">‹</button>
      <span>第 ${state.historyPage} / ${pageCount} 頁 · ${rows.length} 筆</span>
      <div>${pages.map(page => `<button type="button" class="${page === state.historyPage ? "active" : ""}" data-page="${page}" aria-label="第 ${page} 頁">${page}</button>`).join("")}</div>
      <button type="button" data-page="${state.historyPage + 1}" ${state.historyPage === pageCount ? "disabled" : ""} aria-label="下一頁">›</button>
    `;
    $$("#episode-list [data-stock]").forEach(button => button.addEventListener("click", () => openStock(button.dataset.stock)));
    $$("#history-pagination [data-page]").forEach(button => button.addEventListener("click", () => {
      const page = Number(button.dataset.page);
      if (page < 1 || page > pageCount) return;
      state.historyPage = page;
      renderHistory();
      $("#panel-history").scrollIntoView({block: "start", behavior: "smooth"});
    }));
  }

  function stockIndex() {
    const stocks = new Map();
    const add = (code, name) => {
      if (!code && !name) return;
      const key = String(code || name);
      if (!stocks.has(key)) stocks.set(key, {code: String(code || "—"), name: String(name || "—")});
    };
    D.raw.forEach(row => add(row.Underlying_Code, row.Underlying_Name));
    D.mainforce.forEach(row => add(row["母股代號"], row["母股名稱"]));
    D.episodes.forEach(row => add(row["母股代號"], row["母股名稱"]));
    D.currentObservation.forEach(row => add(row["母股代號"], row["母股名稱"]));
    return [...stocks.values()];
  }

  function stockEvents(code) {
    const events = [];
    D.raw.filter(row => String(row.Underlying_Code) === code).forEach(row => events.push({
      date: row.Date, order: 1, kind: "raw", label: "RAW", title: `Raw ${row.Warrant_Code}`,
      detail: `${clean(row.Warrant_Name)} · ${clean(row.Displayed_Multiple)}${isNumeric(row.Displayed_Multiple) ? "x" : ""} · ${clean(row.Trade_Direction)}`,
      note: clean(row.Notes),
    }));
    D.mainforce.filter(row => String(row["母股代號"]) === code).forEach(row => events.push({
      date: row["日期"], order: 0, kind: row["方向"] === "BUY" ? "buy" : "sell", label: row["方向"], title: `${row["方向"]} 主力 #${clean(row["排名"])}`,
      detail: row["可見金額(萬)"] == null ? clean(row["資料完整度"]) : `${number(row["可見金額(萬)"])} 萬 · ${clean(row["資料完整度"])}`,
      note: clean(row["備註"]),
    }));
    const episodes = D.episodes.filter(row => String(row["母股代號"]) === code).sort((a, b) => String(a["進觀察日"] || "").localeCompare(String(b["進觀察日"] || "")));
    episodes.forEach((row, index) => {
      if (row["進觀察日"]) events.push({
        date: row["進觀察日"], order: 2, kind: "enter", label: index === 0 ? "進觀察" : "再次進場", title: index === 0 ? "進入觀察名單" : "再次進入觀察",
        detail: clean(row["資料用途"]), note: clean(row["備註"]),
      });
      if (row["退出日"]) events.push({
        date: row["退出日"], order: 3, kind: "exit", label: "退出", title: "退出觀察名單",
        detail: `歷史報酬 ${formatReturn(row["歷史報酬%"])}`, note: clean(row["備註"]),
      });
    });
    return events.sort((a, b) => a.date.localeCompare(b.date) || a.order - b.order);
  }

  function renderStockSearch() {
    const query = stockSearch.value.trim().toLowerCase();
    const box = $("#stock-result");
    if (!query) {
      state.selectedStockKey = "";
      box.innerHTML = `<div class="empty search-empty">輸入股票代號或名稱，查看完整 Episode timeline</div>`;
      return;
    }
    const matches = stockIndex().filter(stock => textMatch(query, stock.code, stock.name));
    if (!matches.length) {
      box.innerHTML = `<div class="empty">找不到「${esc(stockSearch.value)}」</div>`;
      return;
    }
    const exact = matches.find(stock => stock.code.toLowerCase() === query || stock.name.toLowerCase() === query);
    const selected = matches.find(stock => stock.code === state.selectedStockKey) || exact || matches[0];
    state.selectedStockKey = selected.code;
    const events = stockEvents(selected.code);
    const activeEpisode = D.episodes.find(row => String(row["母股代號"]) === selected.code && row["目前狀態"] === "Active");
    const rawCount = D.raw.filter(row => String(row.Underlying_Code) === selected.code).length;
    const episodeCount = D.episodes.filter(row => String(row["母股代號"]) === selected.code).length;
    const suggestions = matches.length > 1 ? `<div class="stock-suggestions">${matches.map(stock => `<button type="button" class="${stock.code === selected.code ? "active" : ""}" data-select-stock="${esc(stock.code)}">${esc(stock.code)} ${esc(stock.name)}</button>`).join("")}</div>` : "";
    box.innerHTML = `${suggestions}
      <article class="stock-summary panel">
        <div><span class="stock-code">${esc(selected.code)}</span><h3>${esc(selected.name)}</h3></div>
        <span class="badge ${activeEpisode ? "active-status" : "neutral"}">${activeEpisode ? "目前觀察中" : "非目前觀察"}</span>
        <dl><div><dt>歷史 Raw</dt><dd>${rawCount} 張</dd></div><div><dt>Episode</dt><dd>${episodeCount} 段</dd></div><div><dt>事件</dt><dd>${events.length} 筆</dd></div></dl>
      </article>
      <div class="timeline" aria-label="${esc(selected.code)} ${esc(selected.name)} 事件時間線">
        ${events.length ? events.map(event => `<article class="timeline-event ${event.kind} ${event.date === dateSelect.value ? "selected-date" : ""}">
          <div class="timeline-marker"></div>
          <time>${esc(event.date)}</time>
          <div class="timeline-card"><span class="event-type">${esc(event.label)}</span><h4>${esc(event.title)}</h4><p>${esc(event.detail)}</p>${event.note !== "—" ? `<small>${esc(event.note)}</small>` : ""}</div>
        </article>`).join("") : `<div class="empty">此股票目前沒有可串接的事件</div>`}
      </div>`;
    $$('[data-select-stock]').forEach(button => button.addEventListener("click", () => {
      state.selectedStockKey = button.dataset.selectStock;
      renderStockSearch();
    }));
  }

  function openStock(query) {
    stockSearch.value = query;
    state.selectedStockKey = String(query);
    renderStockSearch();
    selectTab("stock");
  }

  function render() {
    const date = dateSelect.value;
    dateSelect.dataset.prev = date;
    const rows = dateRows(date);
    $("#date-note").textContent = D.meta.notes?.[date] || "所選日期資料依目前收錄狀態呈現。";
    renderKpis(rows);
    renderOverview(rows, date);
    renderRaw(rows.raw);
    renderRank(rows.buy, "#buy-rank", "#buy-completeness", "BUY");
    renderRank(rows.sell, "#sell-rank", "#sell-completeness", "SELL");
    selectMainSide(state.mainSide);
    renderObservation(date);
    renderHistory();
    renderStockSearch();
  }

  function setupControls() {
    monthSelect.innerHTML = D.meta.months.map(month => `<option value="${month}">${month}</option>`).join("");
    const defaultDate = D.meta.dates.includes(D.meta.defaultDate) ? D.meta.defaultDate : D.meta.dates[D.meta.dates.length - 1];
    const defaultMonth = defaultDate.slice(0, 7);
    monthSelect.value = defaultMonth;
    const dates = D.meta.dates.filter(date => date.startsWith(defaultMonth));
    dateSelect.innerHTML = dates.map(date => `<option value="${date}">${date}</option>`).join("");
    dateSelect.value = defaultDate;
    dateSelect.dataset.prev = defaultDate;
    renderPills(dates, defaultDate);

    monthSelect.addEventListener("change", () => setMonth(monthSelect.value));
    dateSelect.addEventListener("change", () => {
      dateSelect.dataset.prev = dateSelect.value;
      renderPills(D.meta.dates.filter(date => date.startsWith(monthSelect.value)), dateSelect.value);
      render();
    });
    $$("[data-side]").forEach(button => button.addEventListener("click", () => selectMainSide(button.dataset.side)));
    $$("[data-observation-mode]").forEach(button => button.addEventListener("click", () => {
      state.observationMode = button.dataset.observationMode;
      $$("[data-observation-mode]").forEach(item => item.classList.toggle("active", item === button));
      renderObservation(dateSelect.value);
    }));
    $("#observation-search").addEventListener("input", () => renderObservation(dateSelect.value));
    $("#observation-sort").addEventListener("change", () => renderObservation(dateSelect.value));
    ["#history-search", "#history-outcome", "#history-sort"].forEach(selector => {
      const eventName = selector === "#history-search" ? "input" : "change";
      $(selector).addEventListener(eventName, () => {
        state.historyPage = 1;
        renderHistory();
      });
    });
    stockSearch.addEventListener("input", () => {
      state.selectedStockKey = "";
      renderStockSearch();
    });
  }

  setupNavigation();
  setupControls();
  render();
})();
