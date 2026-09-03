const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..");
const mainHtml = fs.readFileSync(path.join(root, "index.html"), "utf8");
const stockHtml = fs.readFileSync(path.join(root, "stock-strategy", "index.html"), "utf8");
const result = JSON.parse(fs.readFileSync(path.join(root, "stock-strategy", "backtest-result.json"), "utf8"));
const resultV2 = JSON.parse(fs.readFileSync(path.join(root, "stock-strategy", "backtest-result-v2.json"), "utf8"));

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
