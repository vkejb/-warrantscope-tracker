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

  function pricePriority(snapshot) {
    const type = String(snapshot?.price_type || "").toUpperCase();
    return /LIQUIDATION|LIQUIDATE|BID/.test(type) ? 2 : 1;
  }

  function priceMarketDate(snapshot) {
    return String(snapshot?.market_date || snapshot?.captured_at || snapshot?.created_at || "").slice(0, 10);
  }

  function transactionOrder(a, b) {
    return timestamp(a.traded_at) - timestamp(b.traded_at)
      || timestamp(a.created_at) - timestamp(b.created_at)
      || String(a.id || "").localeCompare(String(b.id || ""));
  }

  function effectiveTransactions(transactions) {
    const rows = [...(transactions || [])];
    const supersededIds = new Set(rows.map(row => row.supersedes_transaction_id).filter(Boolean).map(String));
    return rows.filter(row => {
      const status = String(row.status || "CONFIRMED").toUpperCase();
      return status !== "VOID" && !supersededIds.has(String(row.id || ""));
    });
  }

  function priceIndex(priceSnapshots) {
    const latest = new Map();
    [...(priceSnapshots || [])]
      .sort((a, b) => priceMarketDate(b).localeCompare(priceMarketDate(a))
        || pricePriority(b) - pricePriority(a)
        || timestamp(b.captured_at || b.created_at) - timestamp(a.captured_at || a.created_at))
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
      strategyRealizedPnl: 0,
      episodeRealizedPnl: 0,
      episodeBuyLots: 0,
      episodeBuyGross: 0,
      episodeBuyFees: 0,
      episodeFeeInclusiveBuyCost: 0,
      episodeSellLots: 0,
      episodeSellGross: 0,
      episodeSellFees: 0,
      episodeSellTaxes: 0,
      episodeLastSellPrice: null,
      episodeExcludedFromStrategy: false,
      episodeNumber: 0,
      currentEpisodeId: null,
      episodeStartedAt: null,
      lastTradedAt: null,
    };
  }

  function calculatePortfolio(transactions, priceSnapshots = [], tradeEpisodes = []) {
    const states = new Map();
    const closedEpisodes = [];
    const sorted = effectiveTransactions(transactions).sort(transactionOrder);
    const episodeRecords = new Map((tradeEpisodes || []).filter(row => row?.id).map(row => [String(row.id), row]));
    const excludedEpisodeIds = new Set([...episodeRecords.entries()]
      .filter(([, row]) => String(row.signal_tag || "").toUpperCase() === "EXCLUDED_FROM_STRATEGY")
      .map(([id]) => id));

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
          state.episodeBuyLots = 0;
          state.episodeBuyGross = 0;
          state.episodeBuyFees = 0;
          state.episodeFeeInclusiveBuyCost = 0;
          state.episodeSellLots = 0;
          state.episodeSellGross = 0;
          state.episodeSellFees = 0;
          state.episodeSellTaxes = 0;
          state.episodeLastSellPrice = null;
          state.episodeExcludedFromStrategy = excludedEpisodeIds.has(String(transaction.episode_id || ""));
        } else if (transaction.episode_id) {
          state.currentEpisodeId = transaction.episode_id;
          state.episodeExcludedFromStrategy = excludedEpisodeIds.has(String(transaction.episode_id));
        }
        state.lots += lots;
        state.grossBasis += units * price;
        state.feeBasis += units * price + commission + transactionTax;
        state.episodeBuyLots += lots;
        state.episodeBuyGross += units * price;
        state.episodeBuyFees += commission + transactionTax;
        state.episodeFeeInclusiveBuyCost += units * price + commission + transactionTax;
      } else {
        if (lots > state.lots + EPSILON) {
          throw new PortfolioError(
            "OVERSELL",
            `${code} 賣出 ${lots} 張，超過目前持有 ${state.lots} 張。`,
            transaction,
          );
        }
        if (transaction.episode_id) {
          state.currentEpisodeId = transaction.episode_id;
          state.episodeExcludedFromStrategy = excludedEpisodeIds.has(String(transaction.episode_id));
        }
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
        if (!state.episodeExcludedFromStrategy) state.strategyRealizedPnl = currency(state.strategyRealizedPnl + realized);
        state.episodeSellLots += lots;
        state.episodeSellGross += units * price;
        state.episodeSellFees += commission;
        state.episodeSellTaxes += transactionTax;
        state.episodeLastSellPrice = price;

        if (state.lots <= EPSILON) {
          const episodeRecord = episodeRecords.get(String(state.currentEpisodeId || ""));
          const averageBuyPrice = state.episodeBuyLots ? state.episodeBuyGross / (state.episodeBuyLots * UNITS_PER_LOT) : null;
          const averageSellPrice = state.episodeSellLots ? state.episodeSellGross / (state.episodeSellLots * UNITS_PER_LOT) : null;
          closedEpisodes.push({
            warrantCode: code,
            warrantName: state.warrantName,
            underlyingCode: state.underlyingCode,
            underlyingName: state.underlyingName,
            issuer: state.issuer,
            episodeId: state.currentEpisodeId,
            episodeNumber: state.episodeNumber,
            startedAt: state.episodeStartedAt,
            endedAt: transaction.traded_at || null,
            realizedPnl: state.episodeRealizedPnl,
            returnPercent: state.episodeFeeInclusiveBuyCost
              ? state.episodeRealizedPnl / state.episodeFeeInclusiveBuyCost * 100
              : null,
            totalBuyLots: state.episodeBuyLots,
            averageBuyPrice,
            averageSellPrice,
            lastSellPrice: state.episodeLastSellPrice,
            buyFees: state.episodeBuyFees,
            sellFees: state.episodeSellFees,
            sellTaxes: state.episodeSellTaxes,
            feeInclusiveBuyCost: state.episodeFeeInclusiveBuyCost,
            signalTag: episodeRecord?.signal_tag || null,
            excludedFromStrategy: state.episodeExcludedFromStrategy,
            holdingDays: holdingDays(state.episodeStartedAt, transaction.traded_at),
          });
          state.lots = 0;
          state.grossBasis = 0;
          state.feeBasis = 0;
          state.currentEpisodeId = null;
          state.episodeStartedAt = null;
          state.episodeRealizedPnl = 0;
          state.episodeBuyLots = 0;
          state.episodeBuyGross = 0;
          state.episodeBuyFees = 0;
          state.episodeFeeInclusiveBuyCost = 0;
          state.episodeSellLots = 0;
          state.episodeSellGross = 0;
          state.episodeSellFees = 0;
          state.episodeSellTaxes = 0;
          state.episodeLastSellPrice = null;
          state.episodeExcludedFromStrategy = false;
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
          priceType: snapshot?.price_type || null,
          priceCapturedAt: snapshot?.captured_at || snapshot?.created_at || null,
          unrealizedPnl: marketPrice === null ? null : marketPrice * units - state.feeBasis,
        };
      })
      .sort((a, b) => a.underlyingCode.localeCompare(b.underlyingCode) || a.warrantCode.localeCompare(b.warrantCode));

    const realizedPnl = [...states.values()].reduce((sum, state) => sum + state.realizedPnl, 0);
    const feeInclusiveCost = positions.reduce((sum, position) => sum + position.feeBasis, 0);
    const hasAllPrices = positions.every(position => position.unrealizedPnl !== null);
    const unrealizedPnl = hasAllPrices ? positions.reduce((sum, position) => sum + position.unrealizedPnl, 0) : null;
    const strategyPositions = positions.filter(position => !position.episodeExcludedFromStrategy);
    const hasAllStrategyPrices = strategyPositions.every(position => position.unrealizedPnl !== null);
    const strategyRealizedPnl = [...states.values()].reduce((sum, state) => sum + state.strategyRealizedPnl, 0);
    const strategyUnrealizedPnl = hasAllStrategyPrices
      ? strategyPositions.reduce((sum, position) => sum + position.unrealizedPnl, 0)
      : null;

    return {
      positions,
      closedEpisodes,
      realizedPnl,
      feeInclusiveCost,
      unrealizedPnl,
      strategyRealizedPnl,
      strategyUnrealizedPnl,
      strategyCumulativePnl: strategyUnrealizedPnl === null ? null : strategyRealizedPnl + strategyUnrealizedPnl,
      states,
      transactions: sorted,
    };
  }

  function holdingDays(startedAt, endedAt) {
    if (!startedAt || !endedAt) return null;
    const start = new Date(`${String(startedAt).slice(0, 10)}T00:00:00.000Z`).getTime();
    const end = new Date(`${String(endedAt).slice(0, 10)}T00:00:00.000Z`).getTime();
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
    return Math.max(1, Math.round((end - start) / 86400000));
  }

  function mergeClosedEpisodeHistory(tradeEpisodes, ledgerClosedEpisodes) {
    const closed = [...(ledgerClosedEpisodes || [])];
    const consumed = new Set();
    const findLedgerEpisode = episode => {
      const id = String(episode?.id || "");
      let index = closed.findIndex((row, rowIndex) => !consumed.has(rowIndex) && id && String(row.episodeId || "") === id);
      if (index < 0) {
        index = closed.findIndex((row, rowIndex) => !consumed.has(rowIndex)
          && row.warrantCode === String(episode?.warrant_code || "")
          && (!episode?.started_at || String(row.startedAt || "").slice(0, 10) === String(episode.started_at).slice(0, 10))
          && (!episode?.ended_at || String(row.endedAt || "").slice(0, 10) === String(episode.ended_at).slice(0, 10)));
      }
      if (index >= 0) consumed.add(index);
      return index >= 0 ? closed[index] : null;
    };
    const rows = (tradeEpisodes || [])
      .filter(episode => String(episode.status || "").toUpperCase() === "CLOSED" || episode.ended_at)
      .map(episode => {
        const ledgerEpisode = findLedgerEpisode(episode);
        const databasePnl = nullableNumeric(episode.realized_pnl);
        const realizedPnl = databasePnl ?? ledgerEpisode?.realizedPnl ?? null;
        const feeInclusiveBuyCost = ledgerEpisode?.feeInclusiveBuyCost ?? null;
        return {
          id: episode.id || ledgerEpisode?.episodeId || null,
          warrantCode: episode.warrant_code || ledgerEpisode?.warrantCode || "",
          warrantName: episode.warrant_name || ledgerEpisode?.warrantName || "",
          underlyingCode: episode.underlying_code || ledgerEpisode?.underlyingCode || "",
          underlyingName: episode.underlying_name || ledgerEpisode?.underlyingName || "",
          issuer: episode.issuer || ledgerEpisode?.issuer || "",
          startedAt: episode.started_at || ledgerEpisode?.startedAt || null,
          endedAt: episode.ended_at || ledgerEpisode?.endedAt || null,
          totalBuyLots: ledgerEpisode?.totalBuyLots ?? null,
          averageBuyPrice: ledgerEpisode?.averageBuyPrice ?? null,
          averageSellPrice: ledgerEpisode?.averageSellPrice ?? null,
          lastSellPrice: ledgerEpisode?.lastSellPrice ?? null,
          feeInclusiveBuyCost,
          realizedPnl,
          returnPercent: realizedPnl !== null && feeInclusiveBuyCost
            ? realizedPnl / feeInclusiveBuyCost * 100
            : ledgerEpisode?.returnPercent ?? null,
          holdingDays: ledgerEpisode?.holdingDays ?? holdingDays(episode.started_at, episode.ended_at),
          signalTag: episode.signal_tag || ledgerEpisode?.signalTag || null,
          excludedFromStrategy: String(episode.signal_tag || ledgerEpisode?.signalTag || "").toUpperCase() === "EXCLUDED_FROM_STRATEGY",
          notes: episode.notes || "",
          status: "CLOSED",
        };
      });

    closed.forEach((episode, index) => {
      if (consumed.has(index)) return;
      rows.push({...episode, id: episode.episodeId, notes: "", status: "CLOSED"});
    });
    return rows;
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

  function nullableNumeric(value) {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function snapshotDate(row) {
    return String(row?.snapshot_date || "").slice(0, 10);
  }

  function snapshotTotalAssets(row) {
    if (!row) return null;
    const netAssetValue = nullableNumeric(row.net_asset_value);
    if (netAssetValue !== null) return netAssetValue;
    const cashBalance = nullableNumeric(row.cash_balance);
    const pendingSettlement = nullableNumeric(row.pending_settlement) ?? 0;
    const liquidationValue = nullableNumeric(row.position_liquidation_value);
    if (cashBalance === null || liquidationValue === null) return null;
    return cashBalance + pendingSettlement + liquidationValue;
  }

  function dateKeyInTimezone(value, timezone = "Asia/Taipei") {
    if (!value) return "";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value).slice(0, 10);
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(parsed).reduce((values, part) => ({...values, [part.type]: part.value}), {});
    return `${parts.year}-${parts.month}-${parts.day}`;
  }

  function netExternalCashFlow(cashFlows, asOf = "", timezone = "Asia/Taipei") {
    return (cashFlows || []).filter(flow => !asOf || dateKeyInTimezone(flow.occurred_at, timezone) <= asOf).reduce((sum, flow) => {
      const amount = Math.abs(numeric(flow.amount));
      const type = String(flow.flow_type || "").toUpperCase();
      if (type === "DEPOSIT") return sum + amount;
      if (type === "WITHDRAWAL") return sum - amount;
      return sum;
    }, 0);
  }

  function signedCashFlowAmount(flow) {
    const amount = Math.abs(numeric(flow?.amount));
    const type = String(flow?.flow_type || "").toUpperCase();
    return type === "WITHDRAWAL" || type === "INTEREST_EXPENSE" ? -amount : amount;
  }

  function transactionNetCash(transaction) {
    const explicit = nullableNumeric(transaction?.net_cash_amount);
    if (explicit !== null) return explicit;
    const side = String(transaction?.side || "").toUpperCase();
    const lots = numeric(transaction?.lots, NaN);
    const price = numeric(transaction?.price, NaN);
    if (!Number.isFinite(lots) || !Number.isFinite(price) || !["BUY", "SELL"].includes(side)) return null;
    const gross = lots * UNITS_PER_LOT * price;
    const fees = numeric(transaction?.commission) + numeric(transaction?.transaction_tax);
    return side === "BUY" ? -(gross + fees) : gross - fees;
  }

  function settlementNetForDate(transactions, date, timezone = "Asia/Taipei") {
    if (!date) return null;
    return currency(effectiveTransactions(transactions)
      .filter(transaction => dateKeyInTimezone(transaction.traded_at, timezone) === date)
      .reduce((sum, transaction) => sum + (transactionNetCash(transaction) ?? 0), 0));
  }

  function calculateAccountOverview({settings, cashFlows, dailySnapshots, ledger}) {
    const snapshots = [...(dailySnapshots || [])].sort((a, b) => snapshotDate(a).localeCompare(snapshotDate(b)));
    const latestSnapshot = snapshots[snapshots.length - 1] || null;
    const startingCapital = nullableNumeric(settings?.starting_capital);
    const cashBalance = nullableNumeric(latestSnapshot?.cash_balance);
    const pendingSettlement = latestSnapshot ? (nullableNumeric(latestSnapshot.pending_settlement) ?? 0) : null;
    const adjustedCash = cashBalance === null ? null : cashBalance + pendingSettlement;
    const positionMarketValue = nullableNumeric(latestSnapshot?.position_market_value);
    const positionLiquidationValue = nullableNumeric(latestSnapshot?.position_liquidation_value);
    const snapshotNetAssetValue = nullableNumeric(latestSnapshot?.net_asset_value);
    const calculatedTotalAssets = adjustedCash === null || positionLiquidationValue === null
      ? null
      : adjustedCash + positionLiquidationValue;
    const totalAssets = snapshotNetAssetValue ?? calculatedTotalAssets;
    const externalCashFlow = netExternalCashFlow(cashFlows, snapshotDate(latestSnapshot), settings?.timezone || "Asia/Taipei");
    const snapshotTotalPnl = nullableNumeric(latestSnapshot?.total_pnl);
    const ledgerStrategyPnl = nullableNumeric(ledger?.strategyCumulativePnl);
    const cumulativePnl = snapshotTotalPnl ?? ledgerStrategyPnl ?? (totalAssets === null || startingCapital === null
        ? null
        : totalAssets - startingCapital - externalCashFlow);
    const cumulativePerformance = cumulativePnl === null || !startingCapital
      ? null
      : cumulativePnl / startingCapital * 100;
    const realizedPnl = nullableNumeric(latestSnapshot?.realized_pnl)
      ?? nullableNumeric(ledger?.strategyRealizedPnl)
      ?? nullableNumeric(ledger?.realizedPnl);
    const unrealizedPnl = nullableNumeric(latestSnapshot?.unrealized_pnl)
      ?? nullableNumeric(ledger?.strategyUnrealizedPnl)
      ?? nullableNumeric(ledger?.unrealizedPnl);
    const todaySettlement = settlementNetForDate(
      ledger?.transactions || [],
      snapshotDate(latestSnapshot),
      settings?.timezone || "Asia/Taipei",
    );

    return {
      latestSnapshot,
      asOf: snapshotDate(latestSnapshot),
      startingCapital,
      cashBalance,
      pendingSettlement,
      todaySettlement,
      adjustedCash,
      positionMarketValue,
      positionLiquidationValue,
      totalAssets,
      realizedPnl,
      unrealizedPnl,
      externalCashFlow,
      cumulativePnl,
      cumulativePerformance,
    };
  }

  function dateFromKey(key) {
    const parsed = new Date(`${key}T00:00:00.000Z`);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function keyFromDate(date) {
    return date.toISOString().slice(0, 10);
  }

  function startOfWeek(key) {
    const date = dateFromKey(key);
    if (!date) return "";
    const daysSinceMonday = (date.getUTCDay() + 6) % 7;
    date.setUTCDate(date.getUTCDate() - daysSinceMonday);
    return keyFromDate(date);
  }

  function periodSummary(rows, start, end) {
    const withinPeriod = rows.filter(row => {
      const key = snapshotDate(row);
      return key && key >= start && key <= end;
    });
    const completeRows = withinPeriod.filter(row => row.is_complete !== false);
    const dayPnls = completeRows.map(row => nullableNumeric(row.day_pnl)).filter(value => value !== null);
    const dailyReturns = completeRows.map(row => nullableNumeric(row.twr_daily)).filter(value => value !== null);
    return {
      start,
      end,
      snapshotCount: completeRows.length,
      pnl: dayPnls.length ? dayPnls.reduce((sum, value) => sum + value, 0) : null,
      performance: dailyReturns.length ? (dailyReturns.reduce((factor, value) => factor * (1 + value), 1) - 1) * 100 : null,
    };
  }

  function calculatePerformance(dailySnapshots, account) {
    const rows = [...(dailySnapshots || [])].sort((a, b) => snapshotDate(a).localeCompare(snapshotDate(b)));
    const asOf = account?.asOf || snapshotDate(rows[rows.length - 1]);
    if (!asOf) {
      const empty = {start: "", end: "", snapshotCount: 0, pnl: null, performance: null};
      return {week: {...empty}, month: {...empty}, cumulative: {...empty}};
    }
    const week = periodSummary(rows, startOfWeek(asOf), asOf);
    const month = periodSummary(rows, `${asOf.slice(0, 7)}-01`, asOf);
    return {
      week,
      month,
      cumulative: {
        start: snapshotDate(rows[0]),
        end: asOf,
        snapshotCount: rows.length,
        pnl: account?.cumulativePnl ?? null,
        performance: account?.cumulativePerformance ?? null,
      },
    };
  }

  return {
    UNITS_PER_LOT,
    PortfolioError,
    calculatePortfolio,
    calculateAccountOverview,
    calculatePerformance,
    currentPosition,
    effectiveTransactions,
    mergeClosedEpisodeHistory,
    netExternalCashFlow,
    settlementNetForDate,
    signedCashFlowAmount,
    snapshotTotalAssets,
    transactionNetCash,
    validateSale,
  };
});
