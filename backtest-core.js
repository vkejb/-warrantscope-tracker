(function exposeBacktestCore(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.WS_BACKTEST_CORE = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createBacktestCore() {
  "use strict";

  const DEFAULTS = {
    initialCapital: 30000,
    maxPositionPct: 20,
    maxOpenPositions: 3,
    commissionRate: 0.001425,
    commissionDiscount: 0.28,
    minimumCommission: 1,
    sellTaxRate: 0.003,
  };

  const number = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const money = value => Math.round((number(value) + Number.EPSILON) * 100) / 100;

  function transactionCost(gross, side, options) {
    const commission = Math.max(
      options.minimumCommission,
      Math.round(gross * options.commissionRate * options.commissionDiscount),
    );
    const tax = side === "SELL" ? Math.round(gross * options.sellTaxRate) : 0;
    return {commission, tax, total: commission + tax};
  }

  function normalizeEpisode(row, index) {
    const entryPrice = number(row.entryPrice ?? row["進場參考價"], NaN);
    const exitPrice = number(row.exitPrice ?? row["退出參考價"], NaN);
    return {
      id: String(row.id || `${row["母股代號"] || row.code || "trade"}-${index}`),
      code: String(row.code || row["母股代號"] || "").trim(),
      name: String(row.name || row["母股名稱"] || "").trim(),
      entryDate: String(row.entryDate || row["進觀察日"] || "").slice(0, 10),
      exitDate: String(row.exitDate || row["退出日"] || "").slice(0, 10),
      entryPrice,
      exitPrice,
    };
  }

  function validEpisode(row) {
    return row.code && row.entryDate && row.exitDate
      && Number.isFinite(row.entryPrice) && row.entryPrice > 0
      && Number.isFinite(row.exitPrice) && row.exitPrice >= 0;
  }

  function maxDrawdown(points) {
    let peak = points[0]?.equity || 0;
    let worst = 0;
    points.forEach(point => {
      peak = Math.max(peak, point.equity);
      if (peak > 0) worst = Math.max(worst, (peak - point.equity) / peak);
    });
    return worst * 100;
  }

  function runEpisodeBacktest(rows, settings = {}) {
    const options = {...DEFAULTS, ...settings};
    const episodes = (rows || []).map(normalizeEpisode).filter(validEpisode);
    const events = episodes.flatMap(episode => [
      {date: episode.entryDate, type: "ENTRY", episode},
      {date: episode.exitDate, type: "EXIT", episode},
    ]).sort((a, b) => a.date.localeCompare(b.date)
      || (a.type === b.type ? a.episode.code.localeCompare(b.episode.code) : a.type === "EXIT" ? -1 : 1));

    let cash = options.initialCapital;
    const positions = new Map();
    const trades = [];
    const equityCurve = [{date: events[0]?.date || "", equity: cash}];

    events.forEach(event => {
      const {episode} = event;
      if (event.type === "ENTRY") {
        if (positions.size >= options.maxOpenPositions) return;
        const budget = Math.min(cash, options.initialCapital * options.maxPositionPct / 100);
        let shares = Math.floor(budget / episode.entryPrice);
        while (shares > 0) {
          const gross = shares * episode.entryPrice;
          const costs = transactionCost(gross, "BUY", options);
          if (gross + costs.total <= cash && gross + costs.total <= budget) {
            cash = money(cash - gross - costs.total);
            positions.set(episode.id, {episode, shares, entryGross: gross, entryCosts: costs});
            break;
          }
          shares -= 1;
        }
      } else {
        const position = positions.get(episode.id);
        if (!position) return;
        const exitGross = position.shares * episode.exitPrice;
        const exitCosts = transactionCost(exitGross, "SELL", options);
        const proceeds = exitGross - exitCosts.total;
        const invested = position.entryGross + position.entryCosts.total;
        const pnl = money(proceeds - invested);
        cash = money(cash + proceeds);
        trades.push({
          ...episode,
          shares: position.shares,
          invested: money(invested),
          proceeds: money(proceeds),
          pnl,
          returnPct: invested ? pnl / invested * 100 : 0,
          totalCosts: position.entryCosts.total + exitCosts.total,
        });
        positions.delete(episode.id);
      }
      const openValue = [...positions.values()].reduce((sum, position) => sum + position.shares * position.episode.entryPrice, 0);
      equityCurve.push({date: event.date, equity: money(cash + openValue)});
    });

    const wins = trades.filter(trade => trade.pnl > 0);
    const losses = trades.filter(trade => trade.pnl < 0);
    const grossProfit = wins.reduce((sum, trade) => sum + trade.pnl, 0);
    const grossLoss = Math.abs(losses.reduce((sum, trade) => sum + trade.pnl, 0));
    const finalEquity = money(cash + [...positions.values()].reduce((sum, position) => sum + position.shares * position.episode.entryPrice, 0));

    return {
      settings: options,
      sampleCount: episodes.length,
      completedTrades: trades.length,
      skippedTrades: episodes.length - trades.length,
      wins: wins.length,
      losses: losses.length,
      winRate: trades.length ? wins.length / trades.length * 100 : null,
      initialCapital: options.initialCapital,
      finalEquity,
      totalPnl: money(finalEquity - options.initialCapital),
      totalReturnPct: (finalEquity / options.initialCapital - 1) * 100,
      maxDrawdownPct: maxDrawdown(equityCurve),
      profitFactor: grossLoss ? grossProfit / grossLoss : grossProfit > 0 ? Infinity : null,
      trades,
      equityCurve,
      openPositions: [...positions.values()],
    };
  }

  return {DEFAULTS, normalizeEpisode, runEpisodeBacktest, transactionCost};
});
