const test = require("node:test");
const assert = require("node:assert/strict");
const {
  calculatePortfolio,
  calculateAccountOverview,
  calculatePerformance,
  currentPosition,
  effectiveTransactions,
  netExternalCashFlow,
  validateSale,
  PortfolioError,
} = require("../portfolio-core.js");

function trade(overrides) {
  return {
    id: crypto.randomUUID(),
    traded_at: "2026-09-02T01:00:00.000Z",
    warrant_code: "083025",
    warrant_name: "緯穎元大63購11",
    underlying_code: "6669",
    underlying_name: "緯穎",
    issuer: "元大",
    side: "BUY",
    lots: 1,
    price: 1,
    commission: 0,
    transaction_tax: 0,
    episode_id: "episode-1",
    ...overrides,
  };
}

test("部分賣出沿用移動加權平均成本", () => {
  const ledger = calculatePortfolio([
    trade({id: "1", lots: 6, price: 1.6, commission: 20}),
    trade({id: "2", traded_at: "2026-09-02T02:00:00.000Z", lots: 4, price: 2, commission: 20}),
    trade({id: "3", traded_at: "2026-09-02T03:00:00.000Z", side: "SELL", lots: 3, price: 2.2, commission: 10, transaction_tax: 5}),
  ]);
  const position = currentPosition(ledger, "083025");

  assert.equal(position.lots, 7);
  assert.equal(position.averagePrice, 1.76);
  assert.equal(position.averageCostWithFees, 1.764);
  assert.equal(position.feeBasis, 12348);
  assert.equal(position.realizedPnl, 1293);
});

test("持倉歸零後再次買入會建立新的計算 Episode", () => {
  const ledger = calculatePortfolio([
    trade({id: "1", lots: 2, price: 1}),
    trade({id: "2", traded_at: "2026-09-02T02:00:00.000Z", side: "SELL", lots: 2, price: 1.2}),
    trade({id: "3", traded_at: "2026-09-02T03:00:00.000Z", lots: 3, price: 2.5, episode_id: "episode-2"}),
  ]);
  const position = currentPosition(ledger, "083025");

  assert.equal(ledger.closedEpisodes.length, 1);
  assert.equal(position.episodeNumber, 2);
  assert.equal(position.currentEpisodeId, "episode-2");
  assert.equal(position.averagePrice, 2.5);
});

test("禁止賣出超過目前持有張數", () => {
  const ledger = calculatePortfolio([trade({lots: 2})]);
  assert.throws(() => validateSale(ledger, "083025", 3), error => error instanceof PortfolioError && error.code === "OVERSELL");
  assert.throws(() => calculatePortfolio([trade({side: "SELL", lots: 1})]), error => error instanceof PortfolioError && error.code === "OVERSELL");
});

test("使用最新市價計算未實現損益，無市價則等待", () => {
  const withoutPrice = calculatePortfolio([trade({lots: 2, price: 1.5, commission: 10})]);
  assert.equal(withoutPrice.unrealizedPnl, null);

  const withPrice = calculatePortfolio([trade({lots: 2, price: 1.5, commission: 10})], [
    {warrant_code: "083025", price: 1.7, captured_at: "2026-09-02T03:00:00.000Z"},
    {warrant_code: "083025", price: 1.6, captured_at: "2026-09-02T02:00:00.000Z"},
  ]);
  assert.equal(withPrice.positions[0].marketPrice, 1.7);
  assert.equal(withPrice.positions[0].unrealizedPnl, 390);
});

test("排除作廢與已被更正取代的交易，但保留更正後交易", () => {
  const rows = [
    trade({id: "original", lots: 2, status: "CONFIRMED"}),
    trade({id: "replacement", lots: 3, status: "CORRECTED", supersedes_transaction_id: "original"}),
    trade({id: "void", lots: 9, status: "VOID"}),
  ];
  const effective = effectiveTransactions(rows);
  const ledger = calculatePortfolio(rows);

  assert.deepEqual(effective.map(row => row.id), ["replacement"]);
  assert.equal(ledger.positions[0].lots, 3);
});

test("帳戶總覽依交割與外部現金流公式計算", () => {
  const cashFlows = [
    {flow_type: "DEPOSIT", amount: 100000},
    {flow_type: "WITHDRAWAL", amount: 20000},
  ];
  const account = calculateAccountOverview({
    settings: {starting_capital: 1000000},
    cashFlows,
    dailySnapshots: [{
      snapshot_date: "2026-09-02",
      cash_balance: 200000,
      pending_settlement: -50000,
      position_liquidation_value: 900000,
      realized_pnl: 12000,
      unrealized_pnl: -3000,
    }],
    ledger: null,
  });

  assert.equal(netExternalCashFlow(cashFlows), 80000);
  assert.equal(account.adjustedCash, 150000);
  assert.equal(account.totalAssets, 1050000);
  assert.equal(account.cumulativePnl, -30000);
  assert.equal(account.cumulativePerformance, -3);
  assert.equal(account.realizedPnl, 12000);
  assert.equal(account.unrealizedPnl, -3000);
});

test("本週與本月績效以每日損益及 TWR 日報酬串接", () => {
  const snapshots = [
    {snapshot_date: "2026-08-31", day_pnl: 100, twr_daily: 0.01},
    {snapshot_date: "2026-09-01", day_pnl: -20, twr_daily: -0.002},
    {snapshot_date: "2026-09-02", day_pnl: 50, twr_daily: 0.005},
  ];
  const performance = calculatePerformance(snapshots, {
    asOf: "2026-09-02",
    cumulativePnl: 130,
    cumulativePerformance: 0.013,
  });

  assert.equal(performance.week.pnl, 130);
  assert.equal(performance.month.pnl, 30);
  assert.ok(Math.abs(performance.week.performance - 1.30199) < 1e-8);
  assert.equal(performance.cumulative.performance, 0.013);
});
