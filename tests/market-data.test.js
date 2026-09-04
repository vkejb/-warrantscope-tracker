const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

global.window = {};
require("../data.js");
const data = global.window.WS_DATA;

test("2026-09-03 是最新日期且共有 7 張 Raw、5 檔母股", () => {
  assert.equal(data.meta.defaultDate, "2026-09-03");
  assert.equal(data.meta.dates.at(-1), "2026-09-03");

  const raw = data.raw.filter(row => row.Date === "2026-09-03");
  assert.equal(raw.length, 7);
  assert.equal(new Set(raw.map(row => row.Underlying_Code)).size, 5);
  assert.equal(new Set(raw.map(row => row.Issuer)).size, 5);
  assert.deepEqual(raw.map(row => row.Warrant_Code), [
    "065856", "046630", "040002", "712148", "069308", "080393", "064174"
  ]);

  assert.ok(raw.filter(row => row.Underlying_Code === "6669").every(row => row.Trade_Direction === "SELL"));
  assert.equal(raw.find(row => row.Warrant_Code === "712148").Trade_Direction, "BUY");
  assert.equal(raw.find(row => row.Warrant_Code === "080393").Episode_Type, "Fresh Raw→KEEP");
  assert.match(raw.find(row => row.Warrant_Code === "046630").Notes, /5 張.*小分母放大/);
  assert.match(raw.find(row => row.Warrant_Code === "080393").Notes, /50 張.*小分母放大/);

  const incomplete = raw.find(row => row.Warrant_Code === "064174");
  assert.equal(incomplete.Circulation, null);
  assert.equal(incomplete.Displayed_Multiple, null);
});

test("2026-09-03 BUY 與 SELL 排行各有完整 20 筆", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-03");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(sell.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual([buy[0]["母股代號"], buy[0]["可見金額(萬)"]], ["2308", 1901]);
  assert.deepEqual([sell[0]["母股代號"], sell[0]["可見金額(萬)"]], ["1815", 2603]);
  assert.ok(rows.every(row => /僅供近似，不代表完整主力淨額/.test(row["備註"])));
});

test("2026-09-03 當日 Raw 標記符合買賣排行", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-03" && row["當日Raw"] === true);
  assert.deepEqual(rows.map(row => `${row["方向"]}:${row["母股代號"]}`), [
    "BUY:2454", "BUY:2409", "BUY:6147", "BUY:6669", "SELL:6669", "SELL:6147"
  ]);
});

test("觀察清單保留 9/1 新增項目，9/2 為 26 檔、9/3 為 27 檔", () => {
  const snapshot91 = data.observationSnapshots.filter(row => row["日期"] === "2026-09-01");
  const snapshot92 = data.observationSnapshots.filter(row => row["日期"] === "2026-09-02");
  const snapshot93 = data.observationSnapshots.filter(row => row["日期"] === "2026-09-03");
  const added91 = ["6239", "1301", "3406"];

  assert.equal(snapshot91.length, 26);
  assert.ok(added91.every(code => snapshot91.some(row => row["母股代號"] === code && row["進觀察日期"] === "2026-09-01")));
  assert.equal(snapshot92.length, 26);
  assert.equal(snapshot93.length, 27);
  assert.equal(data.currentObservation.length, 27);

  const auo = snapshot93.find(row => row["母股代號"] === "2409");
  assert.equal(auo["進觀察日期"], "2026-09-03");
  assert.equal(auo["當日Raw張數"], 1);
  assert.ok(!snapshot93.some(row => ["8261", "2454"].includes(row["母股代號"])));
});

test("友達保留舊 Episode 並建立 9/3 新 Episode", () => {
  const auoEpisodes = data.episodes.filter(row => row["母股代號"] === "2409");
  assert.equal(auoEpisodes.length, 2);

  const historical = auoEpisodes.find(row => row["目前狀態"] === "Exited");
  assert.deepEqual(
    [historical["進觀察日"], historical["退出日"], historical["歷史報酬%"]],
    ["2026-08-12", "2026-08-20", -1.7]
  );

  const active = auoEpisodes.find(row => row["目前狀態"] === "Active");
  assert.equal(active["進觀察日"], "2026-09-03");
  assert.equal(active["退出日"], null);

  const wiwynn = data.episodes.find(row => row["母股代號"] === "6669" && row["目前狀態"] === "Active");
  assert.match(wiwynn["備註"], /並非可靠的連續 Active 起點/);
});

test("2026-09-02 舊資料完整保留", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-02");
  assert.equal(raw.length, 6);
  assert.deepEqual(raw.map(row => row.Warrant_Code), ["075364", "712105", "082122", "702576", "080492", "074108"]);

  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-02");
  assert.equal(rows.filter(row => row["方向"] === "BUY").length, 20);
  assert.equal(rows.filter(row => row["方向"] === "SELL").length, 20);
});

test("公開市場資料未寫入私人持倉內容", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "data.js"), "utf8");
  assert.doesNotMatch(source, /友達凱基61購12|台塑元大62購01|頎邦群益63購01|金像電元大64購04|緯穎元大63購11|富喬元大61購03/);
});
