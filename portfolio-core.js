(function exposePortfolioCore(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.WS_PORTFOLIO_CORE = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createPortfolioCore() {
  "use strict";

  const UNITS_PER_LOT = 1000;
  const EPSILON = 1e-8;

  class PortfolioError extends Error {
    constructor(code, message, transaction) {
      super(message);
      this.name = "PortfolioError";
      this.code = code;
      this.transaction = transaction;
    }
  }

  function numeric(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function currency(value) {
    return Math.round((value + Number.EPSILON) * 100) / 100;
  }

  function timestamp(value) {
    const parsed = new Date(value || 0).getTime();
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function transactionOrder(a, b) {
    return timestamp(a.traded_at) - timestamp(b.traded_at)
      || timestamp(a.created_at) - timestamp(b.created_at)
      || String(a.id || "").localeCompare(String(b.id || ""));
  }

  function priceIndex(priceSnapshots) {
    const latest = new Map();
    [...(priceSnapshots || [])]
      .sort((a, b) => timestamp(b.captured_at || b.created_at) - timestamp(a.captured_at || a.created_at))
      .forEach(snapshot => {
        const code = String(snapshot.warrant_code || "").trim();
        const price = numeric(snapshot.price, NaN);
        if (code && Number.isFinite(price) && !latest.has(code)) latest.set(code, {...snapshot, price});
      });
    return latest;
  }

  function freshState(transaction) {
    return {
      warrantCode: String(transaction.warrant_code || "").trim(),
      warrantName: String(transaction.warrant_name || "").trim(),
      underlyingCode: String(transaction.underlying_code || "").trim(),
      underlyingName: String(transaction.underlying_name || "").trim(),
      issuer: String(transaction.issuer || "").trim(),
      lots: 0,
      grossBasis: 0,
      feeBasis: 0,
      realizedPnl: 0,
      episodeRealizedPnl: 0,
      episodeNumber: 0,
      currentEpisodeId: null,
      episodeStartedAt: null,
      lastTradedAt: null,
    };
  }

  function calculatePortfolio(transactions, priceSnapshots = []) {
    const states = new Map();
    const closedEpisodes = [];
    const sorted = [...(transactions || [])].sort(transactionOrder);

    sorted.forEach(transaction => {
      const code = String(transaction.warrant_code || "").trim();
      const side = String(transaction.side || "").trim().toUpperCase();
      const lots = numeric(transaction.lots, NaN);
      const price = numeric(transaction.price, NaN);
      const commission = numeric(transaction.commission);
      const transactionTax = numeric(transaction.transaction_tax);

      if (!code || !["BUY", "SELL"].includes(side) || !Number.isFinite(lots) || lots <= 0 || !Number.isFinite(price) || price < 0) {
        throw new PortfolioError("INVALID_TRANSACTION", "交易資料包含無效的代號、方向、張數或價格。", transaction);
      }

      const state = states.get(code) || freshState(transaction);
      state.warrantName = String(transaction.warrant_name || state.warrantName || "").trim();
      state.underlyingCode = String(transaction.underlying_code || state.underlyingCode || "").trim();
      state.underlyingName = String(transaction.underlying_name || state.underlyingName || "").trim();
      state.issuer = String(transaction.issuer || state.issuer || "").trim();
      state.lastTradedAt = transaction.traded_at || state.lastTradedAt;

      const units = lots * UNITS_PER_LOT;
      if (side === "BUY") {
        if (state.lots <= EPSILON) {
          state.lots = 0;
          state.grossBasis = 0;
          state.feeBasis = 0;
          state.episodeNumber += 1;
          state.episodeRealizedPnl = 0;
          state.currentEpisodeId = transaction.episode_id || null;
          state.episodeStartedAt = transaction.traded_at || null;
        } else if (transaction.episode_id) {
          state.currentEpisodeId = transaction.episode_id;
        }
        state.lots += lots;
        state.grossBasis += units * price;
        state.feeBasis += units * price + commission + transactionTax;
      } else {
        if (lots > state.lots + EPSILON) {
          throw new PortfolioError(
            "OVERSELL",
            `${code} 賣出 ${lots} 張，超過目前持有 ${state.lots} 張。`,
            transaction,
          );
        }
        if (transaction.episode_id) state.currentEpisodeId = transaction.episode_id;
        const heldUnits = state.lots * UNITS_PER_LOT;
        const averageGrossCost = heldUnits ? state.grossBasis / heldUnits : 0;
        const averageFeeCost = heldUnits ? state.feeBasis / heldUnits : 0;
        const soldGrossBasis = averageGrossCost * units;
        const soldFeeBasis = averageFeeCost * units;
        const realized = currency(units * price - commission - transactionTax - soldFeeBasis);
        state.lots -= lots;
        state.grossBasis -= soldGrossBasis;
        state.feeBasis -= soldFeeBasis;
        state.realizedPnl = currency(state.realizedPnl + realized);
        state.episodeRealizedPnl = currency(state.episodeRealizedPnl + realized);

        if (state.lots <= EPSILON) {
          closedEpisodes.push({
            warrantCode: code,
            episodeId: state.currentEpisodeId,
            episodeNumber: state.episodeNumber,
            startedAt: state.episodeStartedAt,
            endedAt: transaction.traded_at || null,
            realizedPnl: state.episodeRealizedPnl,
          });
          state.lots = 0;
          state.grossBasis = 0;
          state.feeBasis = 0;
          state.currentEpisodeId = null;
          state.episodeStartedAt = null;
          state.episodeRealizedPnl = 0;
        }
      }

      states.set(code, state);
    });

    const prices = priceIndex(priceSnapshots);
    const positions = [...states.values()]
      .filter(state => state.lots > EPSILON)
      .map(state => {
        const units = state.lots * UNITS_PER_LOT;
        const snapshot = prices.get(state.warrantCode);
        const marketPrice = snapshot ? snapshot.price : null;
        return {
          ...state,
          averagePrice: state.grossBasis / units,
          averageCostWithFees: state.feeBasis / units,
          marketPrice,
          priceCapturedAt: snapshot?.captured_at || snapshot?.created_at || null,
          unrealizedPnl: marketPrice === null ? null : marketPrice * units - state.feeBasis,
        };
      })
      .sort((a, b) => a.underlyingCode.localeCompare(b.underlyingCode) || a.warrantCode.localeCompare(b.warrantCode));

    const realizedPnl = [...states.values()].reduce((sum, state) => sum + state.realizedPnl, 0);
    const feeInclusiveCost = positions.reduce((sum, position) => sum + position.feeBasis, 0);
    const hasAllPrices = positions.every(position => position.unrealizedPnl !== null);
    const unrealizedPnl = hasAllPrices ? positions.reduce((sum, position) => sum + position.unrealizedPnl, 0) : null;

    return {
      positions,
      closedEpisodes,
      realizedPnl,
      feeInclusiveCost,
      unrealizedPnl,
      states,
      transactions: sorted,
    };
  }

  function currentPosition(ledger, warrantCode) {
    return ledger.positions.find(position => position.warrantCode === String(warrantCode || "").trim()) || null;
  }

  function validateSale(ledger, warrantCode, lots) {
    const position = currentPosition(ledger, warrantCode);
    const requested = numeric(lots, NaN);
    if (!position || !Number.isFinite(requested) || requested <= 0 || requested > position.lots + EPSILON) {
      const available = position?.lots || 0;
      throw new PortfolioError("OVERSELL", `目前持有 ${available} 張，不能賣出 ${lots} 張。`);
    }
    return true;
  }

  return {
    UNITS_PER_LOT,
    PortfolioError,
    calculatePortfolio,
    currentPosition,
    validateSale,
  };
});
