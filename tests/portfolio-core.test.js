const test = require("node:test");
const assert = require("node:assert/strict");
const {
  calculatePortfolio,
  currentPosition,
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
