(() => {
  "use strict";

  const SUPABASE_URL = "https://dwnwilahkbbdvhsdiifd.supabase.co";
  const SUPABASE_PUBLISHABLE_KEY = "sb_publishable_xDtObSsw7-jRgjCsUGgQCw_ckK6xUWi";
  const core = window.WS_PORTFOLIO_CORE;
  const byId = id => document.getElementById(id);
  const all = selector => [...document.querySelectorAll(selector)];

  const state = {
    authMode: "login",
    session: null,
    transactions: [],
    prices: [],
    ledger: null,
    requestId: 0,
  };

  let client;

  const escapeHtml = value => String(value ?? "—").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  }[character]));

  const number = value => Number(value || 0).toLocaleString("zh-TW", {maximumFractionDigits: 4});
  const money = value => Number(value || 0).toLocaleString("zh-TW", {
    style: "currency",
    currency: "TWD",
    maximumFractionDigits: 0,
  });
  const signedMoney = value => `${Number(value || 0) > 0 ? "+" : ""}${money(value)}`;
  const price = value => Number(value || 0).toLocaleString("zh-TW", {minimumFractionDigits: 2, maximumFractionDigits: 4});
  const pnlClass = value => Number(value || 0) > 0 ? "profit" : Number(value || 0) < 0 ? "loss" : "flat";

  function localizedError(error, fallback) {
    const message = String(error?.message || "");
    if (/invalid login credentials/i.test(message)) return "Email 或密碼不正確。";
    if (/email not confirmed/i.test(message)) return "請先到信箱完成驗證，再回來登入。";
    if (/user already registered/i.test(message)) return "這個 Email 已註冊，請直接登入。";
    if (/password/i.test(message) && /least|short|characters/i.test(message)) return "密碼長度不足，請至少輸入 6 個字元。";
    if (/fetch|network/i.test(message)) return "目前無法連線到私人資料庫，請稍後再試。";
    return message || fallback;
  }

  function setMessage(target, message = "", tone = "neutral") {
    target.textContent = message;
    target.className = `form-message ${tone}`;
  }

  function setButtonBusy(button, busy, busyLabel) {
    if (busy) {
      button.dataset.label = button.textContent;
      button.textContent = busyLabel;
    } else if (button.dataset.label) {
      button.textContent = button.dataset.label;
      delete button.dataset.label;
    }
    button.disabled = busy;
  }

  function displayDateTime(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return escapeHtml(value);
    return new Intl.DateTimeFormat("zh-TW", {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(parsed);
  }

  function renderSignedOut(message = "請先登入才能讀取私人交易資料。", tone = "neutral") {
    state.session = null;
    state.transactions = [];
    state.prices = [];
    state.ledger = null;
    byId("portfolio-auth").hidden = false;
    byId("portfolio-private").hidden = true;
    byId("portfolio-session-status").textContent = "未登入";
    byId("portfolio-session-status").className = "badge neutral";
    byId("position-list").innerHTML = '<div class="empty">登入後載入持倉</div>';
    byId("transaction-list").innerHTML = '<div class="empty">登入後載入交易</div>';
    closeTradeDialog();
    setMessage(byId("auth-message"), message, tone);
  }

  function renderLoading(session) {
    byId("portfolio-auth").hidden = true;
    byId("portfolio-private").hidden = false;
    byId("portfolio-user-email").textContent = session.user.email || "已登入";
    byId("portfolio-session-status").textContent = "已登入";
    byId("portfolio-session-status").className = "badge active-status";
    byId("portfolio-kpis").innerHTML = ["持倉權證", "含費成本", "已實現損益", "未實現損益"].map(label => `
      <article class="kpi loading-kpi"><span>${label}</span><div><strong>—</strong></div></article>
    `).join("");
    byId("position-list").innerHTML = '<div class="empty">正在載入私人持倉…</div>';
    byId("transaction-list").innerHTML = '<div class="empty">正在載入交易紀錄…</div>';
    setMessage(byId("portfolio-message"), "");
  }

  function renderKpis(ledger) {
    const items = [
      ["持倉權證", ledger.positions.length, "檔", ""],
      ["含費成本", money(ledger.feeInclusiveCost), "", ""],
      ["已實現損益", signedMoney(ledger.realizedPnl), "", pnlClass(ledger.realizedPnl)],
      ["未實現損益", ledger.unrealizedPnl === null ? "等待市價" : signedMoney(ledger.unrealizedPnl), "", ledger.unrealizedPnl === null ? "waiting" : pnlClass(ledger.unrealizedPnl)],
    ];
    byId("portfolio-kpis").innerHTML = items.map(([label, value, unit, className]) => `
      <article class="kpi"><span>${label}</span><div><strong class="${className}">${escapeHtml(value)}</strong>${unit ? `<small>${unit}</small>` : ""}</div></article>
    `).join("");
  }

  function renderPositions(ledger) {
    byId("position-count").textContent = `${ledger.positions.length} 檔`;
    if (!ledger.positions.length) {
      byId("position-list").innerHTML = '<div class="empty">目前沒有持倉。按「＋ 新增交易」記錄第一筆買進。</div>';
      return;
    }
    byId("position-list").innerHTML = ledger.positions.map(position => `
      <article class="position-card">
        <div class="card-title-row">
          <div><span class="stock-code">${escapeHtml(position.warrantCode)}</span><h3>${escapeHtml(position.warrantName || "未命名權證")}</h3></div>
          <span class="badge buy">持有 ${number(position.lots)} 張</span>
        </div>
        <div class="position-context">
          <span>${escapeHtml(position.underlyingCode)} ${escapeHtml(position.underlyingName)}</span>
          <span>${escapeHtml(position.issuer)}</span>
          <span>Episode #${position.episodeNumber}</span>
        </div>
        <dl class="position-metrics">
          <div><dt>持有張數</dt><dd>${number(position.lots)} 張</dd></div>
          <div><dt>平均成交成本</dt><dd>${price(position.averagePrice)}</dd></div>
          <div><dt>含費成本</dt><dd>${money(position.feeBasis)}</dd><small>均價 ${price(position.averageCostWithFees)}</small></div>
          <div><dt>已實現損益</dt><dd class="${pnlClass(position.realizedPnl)}">${signedMoney(position.realizedPnl)}</dd></div>
          <div><dt>最新市價</dt><dd>${position.marketPrice === null ? "等待市價" : price(position.marketPrice)}</dd></div>
          <div><dt>未實現損益</dt><dd class="${position.unrealizedPnl === null ? "waiting" : pnlClass(position.unrealizedPnl)}">${position.unrealizedPnl === null ? "等待市價" : signedMoney(position.unrealizedPnl)}</dd></div>
        </dl>
        <p class="position-footnote">移動加權平均 · 每張 1,000 單位${position.priceCapturedAt ? ` · 市價 ${escapeHtml(displayDateTime(position.priceCapturedAt))}` : ""}</p>
      </article>
    `).join("");
  }

  function renderTransactions(transactions) {
    const rows = [...transactions].sort((a, b) => new Date(b.traded_at || 0) - new Date(a.traded_at || 0));
    byId("transaction-count").textContent = `${rows.length} 筆`;
    if (!rows.length) {
      byId("transaction-list").innerHTML = '<div class="empty">尚無交易紀錄。你的第一筆資料不會由程式預先寫入。</div>';
      return;
    }
    byId("transaction-list").innerHTML = rows.map(transaction => {
      const side = String(transaction.side || "").toUpperCase();
      const value = Number(transaction.lots || 0) * 1000 * Number(transaction.price || 0);
      return `
        <article class="transaction-card">
          <div class="card-title-row">
            <div><span class="stock-code">${escapeHtml(transaction.warrant_code)}</span><h3>${escapeHtml(transaction.warrant_name)}</h3></div>
            <span class="badge ${side === "BUY" ? "buy" : "sell"}">${escapeHtml(side)}</span>
          </div>
          <div class="transaction-meta"><span>${escapeHtml(displayDateTime(transaction.traded_at))}</span><span>${escapeHtml(transaction.underlying_code)} ${escapeHtml(transaction.underlying_name)}</span><span>${escapeHtml(transaction.issuer)}</span></div>
          <dl class="transaction-metrics">
            <div><dt>張數</dt><dd>${number(transaction.lots)} 張</dd></div>
            <div><dt>成交價</dt><dd>${price(transaction.price)}</dd></div>
            <div><dt>成交金額</dt><dd>${money(value)}</dd></div>
            <div><dt>手續費 / 稅</dt><dd>${money(Number(transaction.commission || 0) + Number(transaction.transaction_tax || 0))}</dd></div>
          </dl>
          ${transaction.notes ? `<p class="transaction-note">${escapeHtml(transaction.notes)}</p>` : ""}
        </article>`;
    }).join("");
  }

  function renderLedger() {
    try {
      state.ledger = core.calculatePortfolio(state.transactions, state.prices);
      renderKpis(state.ledger);
      renderPositions(state.ledger);
      renderTransactions(state.transactions);
    } catch (error) {
      setMessage(byId("portfolio-message"), localizedError(error, "無法計算持倉。"), "error");
      byId("position-list").innerHTML = '<div class="empty error-empty">交易資料需要檢查，暫時無法計算持倉。</div>';
      renderTransactions(state.transactions);
    }
  }

  async function loadPrivateData(session, requestedMessage = "") {
    const requestId = ++state.requestId;
    renderLoading(session);
    const userId = session.user.id;
    const [transactionsResult, pricesResult] = await Promise.all([
      client.from("transactions").select("*").eq("user_id", userId).order("traded_at", {ascending: true}),
      client.from("price_snapshots").select("*").eq("user_id", userId).order("captured_at", {ascending: false}),
    ]);
    if (requestId !== state.requestId || state.session?.user?.id !== userId) return;
    if (transactionsResult.error) {
      setMessage(byId("portfolio-message"), localizedError(transactionsResult.error, "讀取交易失敗。"), "error");
      byId("portfolio-kpis").innerHTML = "";
      byId("position-list").innerHTML = '<div class="empty error-empty">無法讀取私人交易資料，請確認 RLS 的 authenticated SELECT policy。</div>';
      byId("transaction-list").innerHTML = "";
      return;
    }
    state.transactions = transactionsResult.data || [];
    state.prices = pricesResult.error ? [] : (pricesResult.data || []);
    renderLedger();
    if (requestedMessage) setMessage(byId("portfolio-message"), requestedMessage, "success");
    else if (pricesResult.error) setMessage(byId("portfolio-message"), "交易已載入；目前無法讀取市價，因此未實現損益顯示「等待市價」。", "warning");
    else setMessage(byId("portfolio-message"), `已安全載入 ${state.transactions.length} 筆私人交易。`, "success");
  }

  async function applySession(session, message = "") {
    state.session = session;
    if (!session?.user) {
      state.requestId += 1;
      renderSignedOut(message || "請先登入才能讀取私人交易資料。", message ? "success" : "neutral");
      return;
    }
    await loadPrivateData(session, message);
  }

  function selectAuthMode(mode) {
    state.authMode = mode;
    all("[data-auth-mode]").forEach(button => {
      const selected = button.dataset.authMode === mode;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    byId("auth-submit").textContent = mode === "signup" ? "建立帳號" : "登入";
    byId("auth-password").autocomplete = mode === "signup" ? "new-password" : "current-password";
    setMessage(byId("auth-message"), mode === "signup" ? "註冊後請到信箱完成驗證，再回來登入。" : "請輸入 Email 與密碼。", "neutral");
  }

  async function handleAuthSubmit(event) {
    event.preventDefault();
    const button = byId("auth-submit");
    const email = byId("auth-email").value.trim();
    const passwordValue = byId("auth-password").value;
    if (!email || passwordValue.length < 6) {
      setMessage(byId("auth-message"), "請輸入有效 Email，密碼至少 6 個字元。", "error");
      return;
    }
    setButtonBusy(button, true, state.authMode === "signup" ? "建立中…" : "登入中…");
    setMessage(byId("auth-message"), "正在安全連線…", "neutral");
    try {
      const result = state.authMode === "signup"
        ? await client.auth.signUp({email, password: passwordValue})
        : await client.auth.signInWithPassword({email, password: passwordValue});
      if (result.error) throw result.error;
      byId("auth-password").value = "";
      if (state.authMode === "signup" && !result.data.session) {
        setMessage(byId("auth-message"), "註冊成功。請到信箱完成驗證，再回來登入。", "success");
      } else if (result.data.session) {
        await applySession(result.data.session, state.authMode === "signup" ? "帳號已建立並登入。" : "登入成功。私人資料已更新。");
      }
    } catch (error) {
      setMessage(byId("auth-message"), localizedError(error, "帳號操作失敗。"), "error");
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function handleLogout() {
    const button = byId("portfolio-logout");
    setButtonBusy(button, true, "登出中…");
    const {error} = await client.auth.signOut({scope: "local"});
    setButtonBusy(button, false);
    if (error) {
      setMessage(byId("portfolio-message"), localizedError(error, "登出失敗。"), "error");
      return;
    }
    await applySession(null, "已登出；私人資料已從畫面移除。");
  }

  function localDateTimeValue(date = new Date()) {
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }

  function selectTradeSide(side) {
    byId("transaction-side").value = side;
    all("[data-trade-side]").forEach(button => {
      const selected = button.dataset.tradeSide === side;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }

  function openTradeDialog() {
    if (!state.session?.user) return;
    const form = byId("transaction-form");
    form.reset();
    byId("transaction-datetime").value = localDateTimeValue();
    selectTradeSide("BUY");
    setMessage(byId("transaction-message"), "");
    const dialog = byId("transaction-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeTradeDialog() {
    const dialog = byId("transaction-dialog");
    if (!dialog) return;
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else dialog.removeAttribute("open");
  }

  async function fetchFreshTransactions(userId) {
    const result = await client.from("transactions").select("*").eq("user_id", userId).order("traded_at", {ascending: true});
    if (result.error) throw result.error;
    return result.data || [];
  }

  async function createEpisode(userId, row, startedAt) {
    const payload = {
      user_id: userId,
      warrant_code: row.warrant_code,
      warrant_name: row.warrant_name,
      underlying_code: row.underlying_code,
      underlying_name: row.underlying_name,
      issuer: row.issuer,
      started_at: startedAt,
      realized_pnl: 0,
      notes: row.notes,
    };
    let result = await client.from("trade_episodes").insert(payload).select("id,status").single();
    if (result.error?.code === "23502" && /status/i.test(result.error.message || "")) {
      result = await client.from("trade_episodes").insert({...payload, status: "OPEN"}).select("id,status").single();
    }
    if (result.error) throw result.error;
    return {...result.data, created: true};
  }

  async function ensureEpisode(userId, row, position) {
    if (position?.currentEpisodeId) return {id: position.currentEpisodeId, status: null, created: false};
    return createEpisode(userId, row, position?.episodeStartedAt || row.traded_at);
  }

  async function closeEpisode(userId, episode, closed) {
    const current = String(episode.status || "");
    const preferred = current.toLowerCase() === "open" ? (current === current.toLowerCase() ? "closed" : "CLOSED") : "CLOSED";
    const candidates = [...new Set([preferred, "CLOSED", "closed", "Closed"])];
    let lastError;
    for (const status of candidates) {
      const result = await client.from("trade_episodes")
        .update({status, ended_at: closed.endedAt, realized_pnl: closed.realizedPnl})
        .eq("user_id", userId)
        .eq("id", episode.id)
        .select("id")
        .maybeSingle();
      if (!result.error && result.data) return;
      lastError = result.error || new Error("Episode 更新未通過 RLS policy。");
      if (result.error?.code !== "23514") break;
    }
    throw lastError;
  }

  async function handleTransactionSubmit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = byId("save-transaction");
    const formData = new FormData(form);
    const localTradedAt = String(formData.get("traded_at") || "");
    const parsedDate = new Date(localTradedAt);
    const row = {
      traded_at: Number.isNaN(parsedDate.getTime()) ? "" : parsedDate.toISOString(),
      warrant_code: String(formData.get("warrant_code") || "").trim(),
      warrant_name: String(formData.get("warrant_name") || "").trim(),
      underlying_code: String(formData.get("underlying_code") || "").trim(),
      underlying_name: String(formData.get("underlying_name") || "").trim(),
      issuer: String(formData.get("issuer") || "").trim(),
      side: String(formData.get("side") || "BUY").toUpperCase(),
      lots: Number(formData.get("lots")),
      price: Number(formData.get("price")),
      commission: Number(formData.get("commission") || 0),
      transaction_tax: Number(formData.get("transaction_tax") || 0),
      notes: String(formData.get("notes") || "").trim() || null,
    };

    if (!form.reportValidity() || !row.traded_at || !["BUY", "SELL"].includes(row.side)) return;
    setButtonBusy(submit, true, "儲存中…");
    setMessage(byId("transaction-message"), "正在確認持倉並寫入私人資料庫…", "neutral");

    let episode = null;
    try {
      const {data: userData, error: userError} = await client.auth.getUser();
      if (userError || !userData.user || userData.user.id !== state.session?.user?.id) throw userError || new Error("登入狀態已失效，請重新登入。");
      const userId = userData.user.id;
      const freshTransactions = await fetchFreshTransactions(userId);
      const freshLedger = core.calculatePortfolio(freshTransactions, state.prices);
      const position = core.currentPosition(freshLedger, row.warrant_code);
      if (row.side === "SELL") core.validateSale(freshLedger, row.warrant_code, row.lots);

      episode = await ensureEpisode(userId, row, position);
      const insertRow = {...row, user_id: userId, episode_id: episode.id};
      const insertResult = await client.from("transactions").insert(insertRow).select("*").single();
      if (insertResult.error) {
        if (episode.created) await client.from("trade_episodes").delete().eq("user_id", userId).eq("id", episode.id);
        throw insertResult.error;
      }

      let closeWarning = "";
      if (row.side === "SELL" && Math.abs((position?.lots || 0) - row.lots) < 1e-8) {
        const afterLedger = core.calculatePortfolio([...freshTransactions, insertResult.data], state.prices);
        const closed = [...afterLedger.closedEpisodes].reverse().find(item => item.episodeId === episode.id);
        if (closed) {
          try {
            await closeEpisode(userId, episode, closed);
          } catch (error) {
            closeWarning = `；交易已儲存，但 Episode 狀態更新失敗：${localizedError(error, "未知錯誤")}`;
          }
        }
      }

      closeTradeDialog();
      await loadPrivateData(state.session, `交易已儲存${closeWarning}`);
    } catch (error) {
      setMessage(byId("transaction-message"), localizedError(error, "儲存交易失敗。"), "error");
    } finally {
      setButtonBusy(submit, false);
    }
  }

  function setupEvents() {
    all("[data-auth-mode]").forEach(button => button.addEventListener("click", () => selectAuthMode(button.dataset.authMode)));
    byId("portfolio-auth-form").addEventListener("submit", handleAuthSubmit);
    byId("portfolio-logout").addEventListener("click", handleLogout);
    byId("open-transaction").addEventListener("click", openTradeDialog);
    byId("close-transaction").addEventListener("click", closeTradeDialog);
    byId("cancel-transaction").addEventListener("click", closeTradeDialog);
    all("[data-trade-side]").forEach(button => button.addEventListener("click", () => selectTradeSide(button.dataset.tradeSide)));
    byId("transaction-form").addEventListener("submit", handleTransactionSubmit);
    byId("transaction-dialog").addEventListener("click", event => {
      if (event.target === byId("transaction-dialog")) closeTradeDialog();
    });
  }

  async function init() {
    if (!window.supabase?.createClient || !core) {
      renderSignedOut("無法載入安全登入元件，請檢查網路後重新整理。", "error");
      return;
    }
    client = window.supabase.createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, {
      auth: {persistSession: true, autoRefreshToken: true, detectSessionInUrl: true},
    });
    setupEvents();
    selectAuthMode("login");
    const {data, error} = await client.auth.getSession();
    if (error) renderSignedOut(localizedError(error, "無法確認登入狀態。"), "error");
    else await applySession(data.session);

    client.auth.onAuthStateChange((event, session) => {
      if (event === "TOKEN_REFRESHED") return;
      window.setTimeout(() => {
        if (event === "SIGNED_IN" && state.session?.access_token === session?.access_token) return;
        if (event === "SIGNED_OUT" && !state.session) return;
        applySession(session, event === "SIGNED_OUT" ? "已登出；私人資料已從畫面移除。" : "");
      }, 0);
    });
  }

  init();
})();
