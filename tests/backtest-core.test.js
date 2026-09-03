const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {runEpisodeBacktest, transactionCost} = require("../backtest-core.js");

test("零股回測納入買賣手續費與賣出交易稅", () => {
  const options = {commissionRate: 0.001425, commissionDiscount: 0.28, minimumCommission: 1, sellTaxRate: 0.003};
  assert.deepEqual(transactionCost(6000, "BUY", options), {commission: 2, tax: 0, total: 2});
  assert.deepEqual(transactionCost(6600, "SELL", options), {commission: 3, tax: 20, total: 23});
});

test("三萬元帳戶依部位上限配置零股並計算勝率", () => {
  const result = runEpisodeBacktest([
    {code: "1111", entryDate: "2026-01-02", exitDate: "2026-01-03", entryPrice: 100, exitPrice: 110},
    {code: "2222", entryDate: "2026-01-04", exitDate: "2026-01-05", entryPrice: 50, exitPrice: 45},
  ], {initialCapital: 30000, maxPositionPct: 20, maxOpenPositions: 1});

  assert.equal(result.completedTrades, 2);
  assert.equal(result.wins, 1);
  assert.equal(result.losses, 1);
  assert.equal(result.winRate, 50);
  assert.ok(result.trades.every(trade => trade.invested <= 6000));
  assert.ok(result.totalPnl < 300);
});

test("同時持倉超過上限時不假設不存在的資金", () => {
  const result = runEpisodeBacktest([
    {code: "1111", entryDate: "2026-01-02", exitDate: "2026-01-05", entryPrice: 100, exitPrice: 110},
    {code: "2222", entryDate: "2026-01-02", exitDate: "2026-01-05", entryPrice: 50, exitPrice: 60},
  ], {maxOpenPositions: 1});

  assert.equal(result.completedTrades, 1);
  assert.equal(result.skippedTrades, 1);
});

test("策略頁已接上回測核心與互動介面", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
  assert.match(html, /data-tab="strategy"/);
  assert.match(html, /id="panel-strategy"/);
  assert.match(html, /backtest-core\.js/);
  assert.match(html, /backtest-ui\.js/);
});
