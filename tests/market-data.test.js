const test = require("node:test");
const assert = require("node:assert/strict");

global.window = {};
require("../data.js");
const data = global.window.WS_DATA;

test("2026-09-02 是最新日期且共有 6 張 Raw", () => {
  assert.equal(data.meta.defaultDate, "2026-09-02");
  assert.equal(data.meta.dates.at(-1), "2026-09-02");

  const raw = data.raw.filter(row => row.Date === "2026-09-02");
  assert.equal(raw.length, 6);
  assert.deepEqual(raw.map(row => row.Warrant_Code), ["075364", "712105", "082122", "702576", "080492", "074108"]);
  assert.equal(raw.find(row => row.Warrant_Code === "075364").Trade_Direction, "SELL");
  assert.equal(raw.find(row => row.Warrant_Code === "080492").Trade_Direction, "SELL");
  assert.match(raw.find(row => row.Warrant_Code === "712105").Notes, /1 張.*小分母放大/);
});

test("2026-09-02 BUY 與 SELL 排行各有完整 20 筆", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-02");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(sell.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.equal(buy[0]["可見金額(萬)"], 4523);
  assert.equal(sell[0]["可見金額(萬)"], 18250);
  assert.ok(rows.every(row => /近似.*不代表完整主力淨額/.test(row["備註"])));
});

test("2026-09-02 當日 Raw 標記符合買賣排行", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-02" && row["當日Raw"] === true);
  assert.deepEqual(rows.map(row => `${row["方向"]}:${row["母股代號"]}`), ["BUY:6669", "SELL:6669", "SELL:1815"]);
});
