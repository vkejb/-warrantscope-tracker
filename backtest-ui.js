(() => {
  "use strict";
  const core = window.WS_BACKTEST_CORE;
  const data = window.WS_DATA;
  if (!core || !data) return;

  const $ = selector => document.querySelector(selector);
  const number = value => Number(value).toLocaleString("zh-TW", {maximumFractionDigits: 2});
  const percent = value => value == null || !Number.isFinite(value) ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
  const esc = value => String(value ?? "—").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  }[char]));

  function settings() {
    return {
      initialCapital: Number($("#bt-capital").value),
      maxPositionPct: Number($("#bt-position-pct").value),
      maxOpenPositions: Number($("#bt-max-positions").value),
      minimumCommission: Number($("#bt-min-fee").value),
    };
  }

  function render() {
    const completed = data.episodes.filter(row => row["退出日"] && row["進場參考價"] != null && row["退出參考價"] != null);
    const result = core.runEpisodeBacktest(completed, settings());
    const kpis = [
      ["期末資產", `$${number(result.finalEquity)}`, `損益 ${result.totalPnl >= 0 ? "+" : ""}$${number(result.totalPnl)}`],
      ["總報酬", percent(result.totalReturnPct), "已扣試算成本"],
      ["勝率", result.winRate == null ? "—" : `${result.winRate.toFixed(1)}%`, `${result.wins} 勝 / ${result.losses} 敗`],
      ["最大回撤", result.maxDrawdownPct == null ? "—" : `${result.maxDrawdownPct.toFixed(2)}%`, "依事件日權益"],
      ["有效樣本", result.completedTrades, "至少需 100 筆"],
    ];
    $("#backtest-kpis").innerHTML = kpis.map(([label, value, note]) => `
      <article class="kpi"><span>${esc(label)}</span><div><strong>${esc(value)}</strong><small>${esc(note)}</small></div></article>
    `).join("");
    $("#backtest-warning").textContent = result.completedTrades < 30
      ? `目前只有 ${result.completedTrades} 筆完成 Episode，結果僅用來驗證計算流程，不能推論未來績效或策略勝率。下一階段需補齊至少 100 筆歷史訊號與每日行情。`
      : "回測仍需進行樣本外測試與紙上交易，不能直接視為實盤報酬。";
    $("#backtest-count").textContent = `${result.completedTrades} 筆`;
    $("#backtest-trades").innerHTML = result.trades.length ? result.trades.map(trade => `
      <article class="backtest-trade">
        <div class="card-title-row"><div><span class="stock-code">${esc(trade.code)}</span><h3>${esc(trade.name)}</h3></div><span class="outcome ${trade.pnl > 0 ? "win" : trade.pnl < 0 ? "loss" : "flat"}">${trade.pnl > 0 ? "勝" : trade.pnl < 0 ? "敗" : "平"}</span></div>
        <div class="transaction-meta"><span>${esc(trade.entryDate)} → ${esc(trade.exitDate)}</span><span>${number(trade.shares)} 股</span><span>成本 $${number(trade.totalCosts)}</span></div>
        <dl class="transaction-metrics"><div><dt>投入</dt><dd>$${number(trade.invested)}</dd></div><div><dt>淨損益</dt><dd class="${trade.pnl >= 0 ? "profit" : "loss"}">${trade.pnl >= 0 ? "+" : ""}$${number(trade.pnl)}</dd></div><div><dt>進出價格</dt><dd>${number(trade.entryPrice)} → ${number(trade.exitPrice)}</dd></div><div><dt>淨報酬</dt><dd>${percent(trade.returnPct)}</dd></div></dl>
      </article>
    `).join("") : '<div class="empty">目前沒有可完成試算的歷史 Episode。</div>';
  }

  $("#run-backtest")?.addEventListener("click", render);
  ["#bt-capital", "#bt-position-pct", "#bt-max-positions", "#bt-min-fee"].forEach(selector => {
    $(selector)?.addEventListener("change", render);
  });
  render();
})();
