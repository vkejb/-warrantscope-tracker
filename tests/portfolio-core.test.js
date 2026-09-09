const test = require("node:test");
const assert = require("node:assert/strict");
const {
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

test("全數賣出後建立完整的 CLOSED Episode 並以含費成本計算報酬", () => {
  const ledger = calculatePortfolio([
    trade({id: "buy", warrant_code: "TEST001", warrant_name: "測試權證A", lots: 2, price: 1.5, commission: 10, episode_id: "episode-a"}),
    trade({id: "sell", warrant_code: "TEST001", warrant_name: "測試權證A", traded_at: "2026-09-08T01:00:00.000Z", side: "SELL", lots: 2, price: 1.7, commission: 10, transaction_tax: 3, episode_id: "episode-a"}),
  ]);
  const episode = ledger.closedEpisodes[0];

  assert.equal(ledger.closedEpisodes.length, 1);
  assert.equal(episode.totalBuyLots, 2);
  assert.equal(episode.averageBuyPrice, 1.5);
  assert.equal(episode.averageSellPrice, 1.7);
  assert.equal(episode.feeInclusiveBuyCost, 3010);
  assert.equal(episode.realizedPnl, 377);
  assert.ok(Math.abs(episode.returnPercent - 377 / 3010 * 100) < 1e-10);
  assert.equal(episode.holdingDays, 6);
});

test("分批賣出直到歸零才形成 CLOSED Episode，多次買賣仍沿用加權成本", () => {
  const firstThree = [
    trade({id: "buy-1", warrant_code: "TEST002", lots: 2, price: 1, commission: 4, episode_id: "episode-b"}),
    trade({id: "buy-2", warrant_code: "TEST002", traded_at: "2026-09-03T01:00:00.000Z", lots: 3, price: 2, commission: 6, episode_id: "episode-b"}),
    trade({id: "sell-1", warrant_code: "TEST002", traded_at: "2026-09-04T01:00:00.000Z", side: "SELL", lots: 2, price: 2.5, commission: 5, transaction_tax: 5, episode_id: "episode-b"}),
  ];
  const partial = calculatePortfolio(firstThree);
  assert.equal(partial.closedEpisodes.length, 0);
  assert.equal(partial.positions[0].lots, 3);
  assert.equal(partial.positions[0].episodeRealizedPnl, 1786);

  const complete = calculatePortfolio([...firstThree,
    trade({id: "sell-2", warrant_code: "TEST002", traded_at: "2026-09-08T01:00:00.000Z", side: "SELL", lots: 3, price: 1.5, commission: 5, transaction_tax: 5, episode_id: "episode-b"}),
  ]);
  assert.equal(complete.positions.length, 0);
  assert.equal(complete.closedEpisodes.length, 1);
  assert.equal(complete.closedEpisodes[0].totalBuyLots, 5);
  assert.equal(complete.closedEpisodes[0].averageBuyPrice, 1.6);
  assert.equal(complete.closedEpisodes[0].averageSellPrice, 1.9);
  assert.equal(complete.closedEpisodes[0].realizedPnl, 1470);
});

test("清倉後重新買入不會與上一輪 Episode 合併", () => {
  const ledger = calculatePortfolio([
    trade({id: "first-buy", warrant_code: "TEST003", lots: 1, price: 1, episode_id: "episode-first"}),
    trade({id: "first-sell", warrant_code: "TEST003", traded_at: "2026-09-03T01:00:00.000Z", side: "SELL", lots: 1, price: 1.2, episode_id: "episode-first"}),
    trade({id: "second-buy", warrant_code: "TEST003", traded_at: "2026-09-08T01:00:00.000Z", lots: 2, price: 2, episode_id: "episode-second"}),
  ]);

  assert.equal(ledger.closedEpisodes.length, 1);
  assert.equal(ledger.closedEpisodes[0].episodeId, "episode-first");
  assert.equal(ledger.positions[0].currentEpisodeId, "episode-second");
  assert.equal(ledger.positions[0].episodeNumber, 2);
  assert.equal(ledger.positions[0].episodeBuyLots, 2);
});

test("EXCLUDED_FROM_STRATEGY 保留歷史損益但不加入策略 aggregate", () => {
  const episodes = [
    {id: "excluded", status: "CLOSED", signal_tag: "EXCLUDED_FROM_STRATEGY", warrant_code: "TEST004", warrant_name: "排除案例"},
    {id: "included", status: "CLOSED", warrant_code: "TEST005", warrant_name: "納入案例"},
  ];
  const ledger = calculatePortfolio([
    trade({id: "ex-buy", warrant_code: "TEST004", lots: 1, price: 1, episode_id: "excluded"}),
    trade({id: "ex-sell", warrant_code: "TEST004", traded_at: "2026-09-03T01:00:00.000Z", side: "SELL", lots: 1, price: 1.2, episode_id: "excluded"}),
    trade({id: "in-buy", warrant_code: "TEST005", traded_at: "2026-09-04T01:00:00.000Z", lots: 1, price: 1, episode_id: "included"}),
    trade({id: "in-sell", warrant_code: "TEST005", traded_at: "2026-09-05T01:00:00.000Z", side: "SELL", lots: 1, price: 1.1, episode_id: "included"}),
  ], [], episodes);
  const history = mergeClosedEpisodeHistory(episodes, ledger.closedEpisodes);

  assert.equal(ledger.realizedPnl, 300);
  assert.equal(ledger.strategyRealizedPnl, 100);
  assert.equal(history.length, 2);
  assert.equal(history.find(row => row.id === "excluded").realizedPnl, 200);
  assert.equal(history.find(row => row.id === "excluded").excludedFromStrategy, true);
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

test("個別持倉優先使用清算或買價快照計算未實現損益", () => {
  const ledger = calculatePortfolio([trade({lots: 2, price: 1.5})], [
    {warrant_code: "083025", price: 1.8, price_type: "LAST", captured_at: "2026-09-08T03:00:00.000Z"},
    {warrant_code: "083025", price: 1.6, price_type: "LIQUIDATION", captured_at: "2026-09-08T02:00:00.000Z"},
  ]);

  assert.equal(ledger.positions[0].marketPrice, 1.6);
  assert.equal(ledger.positions[0].priceType, "LIQUIDATION");
  assert.equal(ledger.positions[0].unrealizedPnl, 200);
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

test("帳戶總覽使用最新 snapshot 的總資產、損益與起始本金績效", () => {
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
      position_market_value: 910000,
      position_liquidation_value: 900000,
      net_asset_value: 1060000,
      realized_pnl: 12000,
      unrealized_pnl: -3000,
      total_pnl: 60000,
    }],
    ledger: null,
  });

  assert.equal(netExternalCashFlow(cashFlows), 80000);
  assert.equal(account.adjustedCash, 150000);
  assert.equal(account.positionMarketValue, 910000);
  assert.equal(account.positionLiquidationValue, 900000);
  assert.equal(account.totalAssets, 1060000);
  assert.equal(snapshotTotalAssets(account.latestSnapshot), 1060000);
  assert.equal(account.cumulativePnl, 60000);
  assert.equal(account.cumulativePerformance, 6);
  assert.equal(account.realizedPnl, 12000);
  assert.equal(account.unrealizedPnl, -3000);
});

test("利息支出以負值顯示，但資料庫維持正數金額", () => {
  assert.equal(signedCashFlowAmount({flow_type: "INTEREST_EXPENSE", amount: 197}), -197);
  assert.equal(signedCashFlowAmount({flow_type: "DIVIDEND", amount: 1250}), 1250);
});

test("今日應收付由當日有效交易的淨現金金額合計", () => {
  const transactions = [
    trade({id: "buy", traded_at: "2026-09-08T01:00:00.000Z", lots: 2, price: 1.5, commission: 10, net_cash_amount: null}),
    trade({id: "sell", traded_at: "2026-09-08T02:00:00.000Z", warrant_code: "TEST003", side: "SELL", lots: 1, price: 4, commission: 8, transaction_tax: 2, net_cash_amount: 3990}),
    trade({id: "old", traded_at: "2026-09-07T01:00:00.000Z", lots: 1, price: 1}),
  ];

  assert.equal(settlementNetForDate(transactions, "2026-09-08"), 980);
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

test("baseline snapshot 的空值不會阻塞後續本月績效", () => {
  const performance = calculatePerformance([
    {snapshot_date: "2026-09-01", day_pnl: null, twr_daily: null, is_complete: true},
    {snapshot_date: "2026-09-02", day_pnl: 100, twr_daily: 0.01, is_complete: true},
    {snapshot_date: "2026-09-03", day_pnl: -25, twr_daily: -0.0025, is_complete: true},
  ], {asOf: "2026-09-03", cumulativePnl: 75, cumulativePerformance: 0.75});

  assert.equal(performance.month.pnl, 75);
  assert.ok(Math.abs(performance.month.performance - 0.7475) < 1e-8);
  assert.equal(performance.month.snapshotCount, 3);
});
