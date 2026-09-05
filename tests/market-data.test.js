const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

global.window = {};
require("../data.js");
const data = global.window.WS_DATA;

test("2026-09-04 是最新日期且清楚標示 No-Trigger Day", () => {
  assert.equal(data.meta.defaultDate, "2026-09-04");
  assert.equal(data.meta.dates.at(-1), "2026-09-04");
  assert.equal(data.raw.filter(row => row.Date === "2026-09-04").length, 0);
  assert.match(data.meta.notes["2026-09-04"], /No-Trigger Day.*Raw 0 張.*0 檔母股.*0 issuer/);
});

test("2026-09-03 仍保留 7 張 Raw、5 檔母股", () => {

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

test("2026-09-04 BUY 與 SELL 排行各有正好 20 筆且沒有 Raw 標記", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-04");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => [row["排名"], row["母股代號"], row["可見金額(萬)"]]), [
    [1, "3583", 1261], [2, "6442", 839], [3, "3008", 835], [4, "2357", 795],
    [5, "3017", 754], [6, "6669", 749], [7, "2327", 729], [8, "3406", 660],
    [9, "6290", 615], [10, "6488", 574], [11, "3661", 561], [12, "2308", 557],
    [13, "4977", 555], [14, "6531", 533], [15, "3231", 526], [16, "3324", 510],
    [17, "2454", 483], [18, "6505", 473], [19, "2408", 463], [20, "2376", 446]
  ]);
  assert.deepEqual(sell.map(row => [row["排名"], row["母股代號"], row["可見金額(萬)"]]), [
    [1, "2368", 2315], [2, "6669", 2044], [3, "3017", 1984], [4, "2345", 1196],
    [5, "3026", 1177], [6, "2308", 947], [7, "1815", 894], [8, "3665", 874],
    [9, "1303", 858], [10, "6271", 803], [11, "3231", 761], [12, "5439", 758],
    [13, "3324", 751], [14, "5371", 738], [15, "4967", 662], [16, "3583", 657],
    [17, "3008", 636], [18, "2454", 568], [19, "2404", 557], [20, "5289", 535]
  ]);
  assert.ok(rows.every(row => row["當日Raw"] === false));
  assert.ok(rows.every(row => /僅供近似，不代表完整主力淨額/.test(row["備註"])));
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

test("2026-09-04 觀察快照原樣延續 27 檔，不新增或退出 Episode", () => {
  const snapshot93 = data.observationSnapshots.filter(row => row["日期"] === "2026-09-03");
  const snapshot94 = data.observationSnapshots.filter(row => row["日期"] === "2026-09-04");

  assert.equal(snapshot94.length, 27);
  assert.deepEqual(
    snapshot94.map(row => row["母股代號"]).sort(),
    snapshot93.map(row => row["母股代號"]).sort()
  );
  assert.ok(snapshot94.every(row => row["當日Raw張數"] === 0 && row["狀態"] === "觀察中"));
  assert.equal(data.currentObservation.length, 27);
  assert.equal(data.episodes.length, 31);
  assert.ok(!data.episodes.some(row => row["進觀察日"] === "2026-09-04"));

  const auo = snapshot94.find(row => row["母股代號"] === "2409");
  const chipbond = snapshot94.find(row => row["母股代號"] === "6147");
  assert.match(auo["備註"], /Neutral follow-through.*保留 9\/3 Active Episode/);
  assert.match(chipbond["備註"], /保留既有 Episode，不新增 retrigger/);
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
