
(() => {
  const D = window.WS_DATA;
  const $ = (s) => document.querySelector(s);
  const monthSelect = $("#month-select"), dateSelect = $("#date-select");
  const search = $("#stock-search");

  const esc = (s) => String(s ?? "—").replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
  const clean = v => (v === null || v === undefined || v === "" || v === "—") ? "—" : v;
  const n = v => typeof v === "number" ? v.toLocaleString("zh-TW") : clean(v);
  const sideBadge = side => {
    const s = String(side || "").toUpperCase();
    if (s.includes("SELL")) return `<span class="badge sell">${esc(side)}</span>`;
    if (s.includes("BUY")) return `<span class="badge buy">${esc(side)}</span>`;
    if (s.includes("EXCHANGE")) return `<span class="badge warn">${esc(side)}</span>`;
    return `<span class="badge neutral">${esc(side || "Unknown")}</span>`;
  };

  function dateRows(date) {
    const raw = D.raw.filter(x => x.Date === date);
    const buy = D.mainforce.filter(x => x["日期"] === date && x["方向"] === "BUY");
    const sell = D.mainforce.filter(x => x["日期"] === date && x["方向"] === "SELL");
    const obs = D.observationSnapshots.filter(x => x["日期"] === date);
    return {raw,buy,sell,obs};
  }

  function setMonth(month, keepDate=false) {
    monthSelect.value = month;
    const dates = D.meta.dates.filter(d => d.startsWith(month));
    dateSelect.innerHTML = dates.map(d => `<option value="${d}">${d}</option>`).join("");
    const current = keepDate && dates.includes(dateSelect.dataset.prev) ? dateSelect.dataset.prev : dates[dates.length-1];
    if (current) { dateSelect.value = current; dateSelect.dataset.prev = current; }
    renderPills(dates, current);
    render();
  }

  function renderPills(dates, current) {
    $("#date-pills").innerHTML = dates.map(d =>
      `<button class="date-pill ${d===current?'active':''}" data-date="${d}">${d.slice(5)}</button>`
    ).join("");
    document.querySelectorAll(".date-pill").forEach(btn => btn.onclick = () => {
      dateSelect.value = btn.dataset.date;
      dateSelect.dataset.prev = btn.dataset.date;
      renderPills(dates, btn.dataset.date);
      render();
    });
  }

  function renderKpis({raw,buy,sell,obs}) {
    const u = new Set(raw.map(x => x.Underlying_Code));
    const items = [
      ["Raw權證", raw.length],
      ["Raw母股", u.size],
      ["買方排行", buy.length],
      ["賣方排行", sell.length],
      ["觀察中", obs.length],
    ];
    $("#kpis").innerHTML = items.map(([k,v]) => `<div class="kpi"><span>${k}</span><strong>${v}</strong></div>`).join("");
  }

  function renderRaw(raw) {
    const groups = {};
    raw.forEach(x => {
      const k = `${x.Underlying_Code}|${x.Underlying_Name}`;
      (groups[k] ||= []).push(x);
    });
    $("#raw-summary").textContent = `${raw.length} 張 / ${Object.keys(groups).length} 檔母股`;
    $("#underlying-cards").innerHTML = Object.entries(groups).map(([k, rows]) => {
      const [code,name] = k.split("|");
      const issuers = [...new Set(rows.map(x => x.Issuer).filter(Boolean))];
      const dirs = [...new Set(rows.map(x => x.Trade_Direction).filter(x => x && x !== "Unknown"))];
      return `<div class="under-card ${rows.length>=3?'hot':''}">
        <strong>${esc(code)} ${esc(name)}</strong>
        <div class="meta">${rows.length} 張 Raw · ${issuers.length} issuer ${dirs.length?`· ${dirs.map(esc).join("/")}`:""}</div>
      </div>`;
    }).join("") || `<div class="empty">這一天沒有 Raw 資料</div>`;

    $("#raw-body").innerHTML = raw.map(x => `<tr>
      <td><b>${esc(x.Underlying_Code)} ${esc(x.Underlying_Name)}</b></td>
      <td>${esc(x.Warrant_Code)}<br><span class="muted">${esc(clean(x.Warrant_Name))}</span></td>
      <td>${esc(clean(x.Issuer))}</td><td>${n(x["30m_Volume"])}</td><td>${n(x.Circulation)}</td><td>${esc(clean(x.Displayed_Multiple))}</td>
      <td>${sideBadge(x.Trade_Direction)}</td><td>${esc(clean(x.Episode_Type))}</td><td>${esc(clean(x.Notes))}</td>
    </tr>`).join("") || `<tr><td colspan="9" class="empty">沒有資料</td></tr>`;
  }

  function rankCompleteness(rows) {
    if (!rows.length) return "尚未收到";
    const s = [...new Set(rows.map(x => x["資料完整度"]).filter(Boolean))];
    return s.join(" / ");
  }

  function renderRank(rows, target, badgeId, side) {
    $(badgeId).textContent = rankCompleteness(rows);
    if (!rows.length) {
      $(target).innerHTML = `<div class="empty">這一天的${side==="BUY"?"買方":"賣方"}排行尚未收錄</div>`;
      return;
    }
    const cls = side==="BUY" ? "buy" : "sell";
    $(target).innerHTML = `<div class="rank-list">${rows.map(x => `
      <div class="rank-row">
        <div class="rank-no">#${esc(clean(x["排名"]))}</div>
        <div><div class="rank-name">${esc(clean(x["母股名稱"]))}</div><div class="rank-code">${esc(clean(x["母股代號"]))}</div></div>
        <div class="rank-amt">${x["可見金額(萬)"] == null ? "—" : `${n(x["可見金額(萬)"])} 萬`}</div>
        <div>${x["當日Raw"]===true ? '<span class="raw-dot">RAW</span>' : ""}</div>
      </div>`).join("")}</div>`;
    $(badgeId).className = `badge ${cls}`;
  }

  function renderObservation(obs) {
    $("#obs-summary").textContent = obs.length ? `${obs.length} 檔` : "沒有完整快照";
    $("#observation-list").innerHTML = obs.length ? obs.map(x => `
      <div class="obs-card">
        <div class="obs-title"><b>${esc(clean(x["母股代號"]))} ${esc(clean(x["母股名稱"]))}</b><span class="badge neutral">${esc(clean(x["Episode類型"]))}</span></div>
        <div class="obs-date">進觀察：${esc(clean(x["進觀察日期"]))} · 當日Raw ${esc(clean(x["當日Raw張數"]))} 張</div>
        <div class="obs-date">${esc(clean(x["備註"]))}</div>
      </div>`).join("") : `<div class="empty">這一天沒有完整的觀察中快照；不會用後來的名單硬補成原始資料。</div>`;
  }

  function renderEpisodes() {
    $("#episode-list").innerHTML = D.episodes.slice().sort((a,b)=>String(b["進觀察日"]||"").localeCompare(String(a["進觀察日"]||""))).map(x => `
      <div class="episode-card">
        <b>${esc(clean(x["母股代號"]))} ${esc(clean(x["母股名稱"]))}</b>
        <div class="line">${esc(clean(x["進觀察日"]))} → ${esc(clean(x["退出日"]))} · ${esc(clean(x["目前狀態"]))}</div>
        <div class="line">${esc(clean(x["備註"]))}</div>
      </div>`).join("");
  }

  function renderStockSearch() {
    const q = search.value.trim().toLowerCase();
    const box = $("#stock-result");
    if (!q) { box.classList.add("hidden"); box.innerHTML=""; return; }
    const matchText = (code,name) => String(code||"").toLowerCase().includes(q) || String(name||"").toLowerCase().includes(q);

    const raw = D.raw.filter(x => matchText(x.Underlying_Code, x.Underlying_Name));
    const mf = D.mainforce.filter(x => matchText(x["母股代號"], x["母股名稱"]));
    const eps = D.episodes.filter(x => matchText(x["母股代號"], x["母股名稱"]));
    const cur = D.currentObservation.filter(x => matchText(x["母股代號"], x["母股名稱"]));

    const sample = raw[0] || mf[0] || eps[0] || cur[0];
    if (!sample) {
      box.classList.remove("hidden");
      box.innerHTML = `<div class="empty">找不到「${esc(search.value)}」</div>`;
      return;
    }
    const code = sample.Underlying_Code || sample["母股代號"];
    const name = sample.Underlying_Name || sample["母股名稱"];
    const events = [];
    raw.forEach(x => events.push([x.Date, `Raw ${x.Displayed_Multiple ?? ""}x · ${x.Trade_Direction || "Unknown"}`]));
    mf.forEach(x => events.push([x["日期"], `${x["方向"]} #${x["排名"] ?? "?"}`]));
    eps.forEach(x => {
      if (x["進觀察日"]) events.push([x["進觀察日"], "Entered Observation"]);
      if (x["退出日"]) events.push([x["退出日"], "Exit"]);
    });
    events.sort((a,b)=>String(a[0]).localeCompare(String(b[0])));

    box.classList.remove("hidden");
    box.innerHTML = `<div class="eyebrow">STOCK TIMELINE</div><div class="stock-title">${esc(code)} ${esc(name)}</div>
      <div class="muted">${cur.length ? `目前在觀察中 · 進觀察 ${esc(cur[0]["列入觀察日"] || cur[0]["進觀察日期"] || "—")}` : "目前觀察狀態未確認"}</div>
      <div class="stock-events">${events.map(([d,e])=>`<span class="event-chip"><b>${esc(d)}</b> · ${esc(e)}</span>`).join("") || '<span class="event-chip">尚無事件</span>'}</div>`;
  }

  function render() {
    const date = dateSelect.value;
    dateSelect.dataset.prev = date;
    const rows = dateRows(date);
    $("#date-note").textContent = D.meta.notes?.[date] || "";
    renderKpis(rows);
    renderRaw(rows.raw);
    renderRank(rows.buy, "#buy-rank", "#buy-completeness", "BUY");
    renderRank(rows.sell, "#sell-rank", "#sell-completeness", "SELL");
    renderObservation(rows.obs);
    renderStockSearch();
  }

  // Init selectors
  monthSelect.innerHTML = D.meta.months.map(m => `<option value="${m}">${m}</option>`).join("");
  const defaultDate = D.meta.dates.includes(D.meta.defaultDate) ? D.meta.defaultDate : D.meta.dates[D.meta.dates.length-1];
  const defaultMonth = defaultDate.slice(0,7);
  monthSelect.value = defaultMonth;
  const initDates = D.meta.dates.filter(d => d.startsWith(defaultMonth));
  dateSelect.innerHTML = initDates.map(d => `<option value="${d}">${d}</option>`).join("");
  dateSelect.value = defaultDate;
  dateSelect.dataset.prev = defaultDate;
  renderPills(initDates, defaultDate);

  monthSelect.addEventListener("change", () => setMonth(monthSelect.value));
  dateSelect.addEventListener("change", () => {
    dateSelect.dataset.prev = dateSelect.value;
    renderPills(D.meta.dates.filter(d => d.startsWith(monthSelect.value)), dateSelect.value);
    render();
  });
  search.addEventListener("input", renderStockSearch);

  renderEpisodes();
  render();
})();
