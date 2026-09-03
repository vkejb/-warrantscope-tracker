const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..");
const mainHtml = fs.readFileSync(path.join(root, "index.html"), "utf8");
const stockHtml = fs.readFileSync(path.join(root, "stock-strategy", "index.html"), "utf8");
const result = JSON.parse(fs.readFileSync(path.join(root, "stock-strategy", "backtest-result.json"), "utf8"));
const resultV2 = JSON.parse(fs.readFileSync(path.join(root, "stock-strategy", "backtest-result-v2.json"), "utf8"));
const research = JSON.parse(fs.readFileSync(path.join(root, "stock-strategy", "research-variants.json"), "utf8"));
const eventResearch = JSON.parse(fs.readFileSync(path.join(root, "stock-strategy", "research-event-driven.json"), "utf8"));
const dailyResearch = JSON.parse(fs.readFileSync(path.join(root, "stock-strategy", "research-daily-strength.json"), "utf8"));

test("現股策略與 WarrantScope UI 完全分離", () => {
  assert.doesNotMatch(mainHtml, /panel-strategy|backtest-core|backtest-ui/);
  assert.match(stockHtml, /台股零股策略研究室/);
});

test("回測期間、股票池與成本資料完整", () => {
  assert.equal(result.meta.period, "2020-01-01 ~ 2025-12-31");
  assert.equal(result.meta.warmup, "2019");
  assert.ok(result.meta.universe_count > 1000);
  assert.equal(result.annual.length, 6);
  assert.ok(result.summary.trades > 100);
  assert.ok(result.summary.total_costs > 0);
});

test("結果誠實保留未通過的基準策略", () => {
  assert.ok(result.summary.total_return_pct < 0);
  assert.ok(result.summary.cagr_pct < result.benchmark.cagr_pct);
  assert.ok(result.summary.max_drawdown_pct > result.benchmark.max_drawdown_pct);
});

test("V2 保留設計、驗證與樣本外分期且未用 2025 回頭調參", () => {
  assert.deepEqual(resultV2.meta.phases, {
    design: "2020-2022",
    validation: "2023-2024",
    out_of_sample: "2025",
  });
  assert.equal(resultV2.annual.find(row => row.year === "2025").phase, "out_of_sample");
  assert.ok(resultV2.summary.trades < result.summary.trades);
  assert.ok(resultV2.summary.total_return_pct < 0);
});

test("歸因研究只含預先定義版本並找出較佳但仍落後0050的方向", () => {
  assert.equal(research.variants.length, 5);
  const best = research.variants.find(row => row.id === "low_vol_10_q");
  assert.ok(best.summary.total_return_pct > 0);
  assert.ok(best.phase.validation_2024 > 0);
  assert.ok(best.summary.total_return_pct < resultV2.benchmark.total_return_pct);
});

test("事件退出不因月份到期強制賣股且保留預先定義的三組規則", () => {
  assert.match(eventResearch.method, /no calendar-forced liquidation/);
  assert.equal(eventResearch.variants.length, 3);
  const balanced = eventResearch.variants.find(row => row.id === "trend100_trail10");
  assert.ok(balanced.summary.total_return_pct > 0);
  assert.ok(balanced.summary.max_drawdown_pct < 25);
  assert.ok(balanced.annual["2024"] > 0);
  assert.ok(balanced.annual["2025"] > 0);
});

test("每日強勢研究保留真實成本並標示事後追加診斷不得選模", () => {
  assert.equal(dailyResearch.variants.length, 5);
  assert.match(dailyResearch.method, /not eligible for model selection/);
  const fill = dailyResearch.variants.find(row => row.id === "daily_fill");
  const mechanical = dailyResearch.variants.find(row => row.id === "daily_top10");
  assert.ok(fill.summary.total_costs > 0);
  assert.ok(mechanical.summary.trades > fill.summary.trades);
  assert.ok(mechanical.summary.total_return_pct < fill.summary.total_return_pct);
});
