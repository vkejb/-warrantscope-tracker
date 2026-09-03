(() => {
  "use strict";
  const $ = selector => document.querySelector(selector);
  const money = value => `$${Number(value).toLocaleString("zh-TW", {maximumFractionDigits: 0})}`;
  const pct = value => `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}%`;
  const esc = value => String(value ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));

  function equityChart(points) {
    const width = 760, height = 220, pad = 28;
    const values = points.map(point => point.equity);
    const min = Math.min(...values), max = Math.max(...values), range = max - min || 1;
    const coords = points.map((point, index) => ({
      x: pad + index / Math.max(1, points.length - 1) * (width - pad * 2),
      y: height - pad - (point.equity - min) / range * (height - pad * 2),
    }));
    const line = coords.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
    const area = `${line} L${coords.at(-1).x},${height-pad} L${coords[0].x},${height-pad} Z`;
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="資產曲線">
      <defs><linearGradient id="area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#54e6a2" stop-opacity=".28"/><stop offset="1" stop-color="#54e6a2" stop-opacity="0"/></linearGradient></defs>
      <line class="axis" x1="${pad}" y1="${height-pad}" x2="${width-pad}" y2="${height-pad}"/><path class="area" d="${area}"/><path class="line" d="${line}"/>
      <text x="${pad}" y="${height-8}">${points[0].date.slice(0,4)}</text><text text-anchor="end" x="${width-pad}" y="${height-8}">${points.at(-1).date.slice(0,4)}</text><text x="${pad}" y="14">${money(max)}</text><text x="${pad}" y="${height-pad-5}">${money(min)}</text>
    </svg>`;
  }

  function renderTrades(data, year = "all") {
    const trades = data.trades.filter(trade => year === "all" || trade.exit_date.startsWith(year)).slice().reverse();
    $("#trade-body").innerHTML = trades.map(trade => `<tr><td><strong>${esc(trade.code)}</strong> ${esc(trade.name)}</td><td>${trade.entry_date}</td><td>${trade.exit_date}</td><td>${trade.shares}</td><td>${money(trade.costs)}</td><td class="${trade.pnl >= 0 ? "positive" : "negative"}">${trade.pnl >= 0 ? "+" : ""}${money(trade.pnl)}</td><td class="${trade.return_pct >= 0 ? "positive" : "negative"}">${pct(trade.return_pct)}</td></tr>`).join("");
  }

  Promise.all([
    fetch("backtest-result.json").then(response => response.json()),
    fetch("backtest-result-v2.json").then(response => response.json()),
    fetch("research-variants.json").then(response => response.json()),
  ]).then(([v1, data, research]) => {
    const s = data.summary, b = data.benchmark;
    $("#verdict").textContent = s.total_return_pct > 0 && s.cagr_pct > b.cagr_pct ? "通過基準" : "策略未通過";
    $("#verdict").classList.add(s.total_return_pct > 0 && s.cagr_pct > b.cagr_pct ? "pass" : "fail");
    const outOfSample = data.annual.find(row => row.year === "2025");
    const best = research.variants.find(row => row.id === "low_vol_10_q");
    $("#conclusion").textContent = `五個預先定義版本中，最佳為「${best.name}」：總報酬 ${pct(best.summary.total_return_pct)}、2024驗證 ${pct(best.phase.validation_2024)}，但2025壓力測試 ${pct(best.phase.stress_2025)}，仍遠低於0050的 ${pct(b.total_return_pct)}。結論是增加分散、季度換股與大盤濾網確實改善結果，但純價量選股不適合取代ETF核心。`;
    $("#period").textContent = data.meta.period;
    const items = [
      ["期末資產", money(s.final_equity), `起始 ${money(s.initial_capital)}`],
      ["總報酬", pct(s.total_return_pct), `0050 ${pct(b.total_return_pct)}`],
      ["年化報酬", pct(s.cagr_pct), `0050 ${pct(b.cagr_pct)}`],
      ["最大回撤", `${s.max_drawdown_pct.toFixed(2)}%`, `0050 ${b.max_drawdown_pct.toFixed(2)}%`],
      ["勝率", `${s.win_rate_pct.toFixed(2)}%`, `${s.wins} 勝 / ${s.trades-s.wins} 敗`],
      ["交易成本", money(s.total_costs), `${s.trades} 筆完成交易`],
    ];
    $("#kpis").innerHTML = items.map(([label,value,note], i) => `<article class="kpi"><span>${label}</span><strong class="${i>0&&i<4&&s.total_return_pct<0?"negative":""}">${value}</strong><small>${note}</small></article>`).join("");
    const comparison = [
      ["V1 價量動能", v1.summary],
      ["V2 濾網低換手", s],
      ...research.variants.map(row => [row.name, row.summary]),
      ["0050 價格報酬", {total_return_pct:b.total_return_pct,cagr_pct:b.cagr_pct,max_drawdown_pct:b.max_drawdown_pct,trades:"—"}],
    ];
    $("#comparison").innerHTML = `<div class="comparison-row header"><span>版本</span><span>總報酬</span><span>年化</span><span>最大回撤</span><span>交易數</span></div>${comparison.map(([name,row])=>`<div class="comparison-row"><strong>${name}</strong><span class="${row.total_return_pct>=0?"positive":"negative"}">${pct(row.total_return_pct)}</span><span>${pct(row.cagr_pct)}</span><span>${Number(row.max_drawdown_pct).toFixed(2)}%</span><span>${row.trades}</span></div>`).join("")}`;
    $("#equity-chart").innerHTML = equityChart(data.equity_curve);
    const maxAnnual = Math.max(...data.annual.map(row => Math.abs(row.return_pct)));
    $("#annual-bars").innerHTML = data.annual.map(row => `<div class="annual-row"><span>${row.year}</span><div class="bar-track"><div class="bar ${row.return_pct<0?"loss":""}" style="width:${Math.abs(row.return_pct)/maxAnnual*100}%"></div></div><strong class="${row.return_pct>=0?"positive":"negative"}">${pct(row.return_pct)}</strong></div>`).join("");
    $("#rules").innerHTML = data.strategy.rules.map(rule => `<li>${esc(rule)}</li>`).join("");
    $("#data-notes").innerHTML = `<dt>資料來源</dt><dd><a href="${esc(data.meta.source_url)}" target="_blank" rel="noreferrer">TWSE / TPEX 年度彙整</a></dd><dt>股票池</dt><dd>${data.meta.universe_count.toLocaleString()} 檔歷史普通股</dd><dt>回測期間</dt><dd>${data.meta.period}（2019 暖機）</dd><dt>股息</dt><dd>未計入，屬價格報酬</dd><dt>公司行動</dt><dd>${esc(data.meta.corporate_action_method)}</dd><dt>判定</dt><dd class="negative">未達實盤標準</dd>`;
    data.annual.forEach(row => $("#year-filter").insertAdjacentHTML("beforeend", `<option value="${row.year}">${row.year}</option>`));
    $("#year-filter").addEventListener("change", event => renderTrades(data, event.target.value));
    renderTrades(data);
  }).catch(error => {
    $("#conclusion").textContent = `無法載入回測結果：${error.message}`;
    $("#verdict").textContent = "載入失敗";
    $("#verdict").classList.add("fail");
  });
})();
