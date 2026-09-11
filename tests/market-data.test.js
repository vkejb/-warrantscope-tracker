const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

global.window = {};
require("../data.js");
const data = global.window.WS_DATA;

test("2026-09-11 是最新日期，9/4 No-Trigger Day 仍完整保留", () => {
  assert.equal(data.meta.defaultDate, "2026-09-11");
  assert.equal(data.meta.dates.at(-1), "2026-09-11");
  assert.equal(data.raw.filter(row => row.Date === "2026-09-04").length, 0);
  assert.match(data.meta.notes["2026-09-04"], /No-Trigger Day.*Raw 0 張.*0 檔母股.*0 issuer/);
});

test("2026-09-11 Raw 正好 4 張，國票資料與缺值、方向完整保留", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-11");

  assert.equal(raw.length, 4);
  assert.deepEqual(raw.map(row => row.Warrant_Code), ["079787", "707632", "708743", "712574"]);
  assert.deepEqual(raw.map(row => row.Underlying_Code), ["6830", "3374", "3624", "3105"]);
  assert.deepEqual(raw.map(row => row.Issuer), ["國票", "凱基", "永豐", "國泰"]);
  assert.ok(raw.every(row => row["30m_Volume"] === null && row.Circulation === null && row.Displayed_Multiple === null));
  assert.deepEqual(raw.map(row => row.Trade_Direction), ["SELL", "SELL", "SELL", "SELL-lean / Two-way"]);
  assert.deepEqual(
    raw.map(row => [row.Prior_Buy_Rank, row.Prior_Sell_Rank]),
    [[null, 15], [null, 2], [null, 10], [13, 8]]
  );
  assert.match(raw.find(row => row.Warrant_Code === "712574").Notes, /使用者確認 9\/11 列入觀察/);
});

test("2026-09-11 BUY 與 SELL Top20 完整，分點加總與 Raw 標記正確", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-11");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(sell.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(
    buy.map(row => [row["母股代號"], row["可見金額(萬)"]]),
    [
      ["2308", 1477], ["4958", 1196], ["3406", 940], ["2330", 790], ["6531", 786],
      ["2303", 694], ["3533", 691], ["3583", 667], ["5289", 599], ["3231", 579],
      ["2360", 535], ["2351", 525], ["3105", 517], ["6488", 504], ["6903", 502],
      ["3008", 435], ["2049", 404], ["7734", 394], ["0050", 386], ["6179", 384]
    ]
  );
  assert.deepEqual(
    sell.map(row => [row["母股代號"], row["可見金額(萬)"]]),
    [
      ["1560", 2535], ["3374", 1176], ["2303", 814], ["3406", 773], ["6147", 769],
      ["3661", 767], ["3017", 705], ["3105", 667], ["1815", 657], ["3624", 655],
      ["2409", 604], ["3711", 551], ["2454", 532], ["2368", 527], ["6830", 511],
      ["2345", 458], ["2327", 442], ["6770", 435], ["2344", 432], ["1303", 407]
    ]
  );
  assert.ok(rows.every(row => row["分點1可見金額(萬)"] + row["分點2可見金額(萬)"] === row["可見金額(萬)"]));
  assert.deepEqual(
    rows.filter(row => row["當日Raw"] === true).map(row => `${row["方向"]}:${row["母股代號"]}`),
    ["BUY:3105", "SELL:3374", "SELL:3105", "SELL:3624", "SELL:6830"]
  );
  assert.ok(rows.every(row => /兩個分點加總近似值/.test(row["備註"])));
});

test("2026-09-10 Raw 正好 6 張且缺值、方向與順序符合更新包", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-10");

  assert.equal(raw.length, 6);
  assert.deepEqual(raw.map(row => row.Warrant_Code), ["713557", "065856", "712550", "049309", "052771", "071061"]);
  assert.deepEqual(raw.map(row => row.Underlying_Code), ["5536", "6669", "3211", "2357", "1303", "2454"]);
  assert.ok(raw.every(row => row["30m_Volume"] === null && row.Circulation === null && row.Displayed_Multiple === null));
  assert.deepEqual(raw.map(row => row.Trade_Direction), ["BUY", "SELL", "BUY", "Unknown", "BUY", "SELL"]);
  assert.match(raw.find(row => row.Warrant_Code === "713557").Notes, /使用者確認已於 9\/10 列入觀察/);
  assert.match(raw.find(row => row.Warrant_Code === "065856").Notes, /1\.63 倍.*偏空/);
  assert.match(raw.find(row => row.Warrant_Code === "712550").Episode_Type, /Mature Episode Retrigger/);
  assert.match(raw.find(row => row.Warrant_Code === "049309").Notes, /方向維持 Unknown/);
  assert.match(raw.find(row => row.Warrant_Code === "052771").Notes, /不因下單券商偏好刪除/);
  assert.match(raw.find(row => row.Warrant_Code === "071061").Notes, /2\.86 倍.*偏空/);
});

test("2026-09-10 BUY 與 SELL Top20 完整，名次、金額與 Raw 標記正確", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-10");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(sell.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(
    [buy[0], buy[3], buy[6], buy[7], buy[9], buy[19]].map(row => [row["母股代號"], row["可見金額(萬)"]]),
    [["2303", 1071], ["2409", 817], ["6147", 642], ["3211", 580], ["5536", 550], ["2449", 359]]
  );
  assert.deepEqual(
    [sell[0], sell[8], sell[12], sell[19]].map(row => [row["母股代號"], row["可見金額(萬)"]]),
    [["2454", 1632], ["6669", 865], ["4958", 593], ["3260", 429]]
  );
  assert.deepEqual(
    rows.filter(row => row["當日Raw"] === true).map(row => `${row["方向"]}:${row["母股代號"]}`),
    ["BUY:3211", "BUY:2454", "BUY:5536", "BUY:6669", "BUY:1303", "SELL:2454", "SELL:6669"]
  );
  assert.ok(rows.every(row => /僅供近似，不代表完整主力淨額/.test(row["備註"])));
});

test("2026-09-09 Raw 正好 7 張且缺值、方向與順序符合更新包", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-09");

  assert.equal(raw.length, 7);
  assert.deepEqual(raw.map(row => row.Warrant_Code), [
    "083921", "076115", "083613", "080035", "712637", "072500", "711138"
  ]);
  assert.equal(raw.filter(row => row.Underlying_Code === "2408").length, 3);
  assert.ok(raw.every(row => row["30m_Volume"] === null && row.Circulation === null && row.Displayed_Multiple === null));
  assert.deepEqual(raw.map(row => row.Trade_Direction), ["BUY", "BUY", "BUY", "Unknown", "Unknown", "BUY", "Unknown"]);
  assert.ok(raw.filter(row => row.Underlying_Code === "2408").every(row => /6\.66 倍/.test(row.Notes)));
  assert.match(raw.find(row => row.Warrant_Code === "080035").Notes, /不自動加入 Watch/);
  assert.match(raw.find(row => row.Warrant_Code === "711138").Notes, /不另開新 Episode/);
});

test("2026-09-09 BUY 與 SELL Top20 完整，名次、金額與 Raw 標記正確", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-09");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(sell.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(buy.slice(0, 4).map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["2408", 3579], ["5289", 2140], ["2337", 1764], ["2409", 1019]
  ]);
  assert.deepEqual([buy[19]["母股代號"], buy[19]["可見金額(萬)"]], ["4958", 506]);
  assert.deepEqual(sell.slice(0, 2).map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["6239", 3373], ["3706", 1285]
  ]);
  assert.deepEqual([sell[13]["母股代號"], sell[13]["可見金額(萬)"], sell[14]["母股代號"], sell[14]["可見金額(萬)"]], [
    "2408", 537, "2409", 515
  ]);
  assert.deepEqual(
    rows.filter(row => row["當日Raw"] === true).map(row => `${row["方向"]}:${row["母股代號"]}`),
    ["BUY:2408", "BUY:2301", "SELL:2408"]
  );
  assert.ok(rows.every(row => /僅供近似，不代表完整主力淨額/.test(row["備註"])));
});

test("2026-09-08 Raw 正好 3 張且未提供欄位維持缺值", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-08");

  assert.equal(raw.length, 3);
  assert.deepEqual(raw.map(row => row.Warrant_Code), ["080083", "076856", "082534"]);
  assert.deepEqual(raw.map(row => row.Underlying_Code), ["6426", "6669", "2481"]);
  assert.ok(raw.every(row => row["30m_Volume"] === null && row.Circulation === null && row.Displayed_Multiple === null));
  assert.ok(raw.every(row => row.Trade_Direction === "BUY"));
  assert.match(raw.find(row => row.Warrant_Code === "076856").Notes, /方向反轉/);
  assert.match(raw.find(row => row.Warrant_Code === "080083").Notes, /不自動加入 Watch/);
  assert.match(raw.find(row => row.Warrant_Code === "082534").Notes, /不自動加入 Watch/);
});

test("2026-09-08 BUY 與 SELL Top20 完整且 Raw 標記正確", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-08");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(sell.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(buy.slice(0, 3).map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["2409", 1051], ["6669", 1031], ["3008", 973]
  ]);
  assert.deepEqual(sell.slice(0, 3).map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["2059", 2220], ["2454", 1673], ["1301", 1247]
  ]);
  assert.deepEqual(
    rows.filter(row => row["當日Raw"] === true).map(row => `${row["方向"]}:${row["母股代號"]}`),
    ["BUY:6669", "BUY:6426", "BUY:2481"]
  );
  assert.ok(rows.every(row => /僅供近似，不代表完整主力淨額/.test(row["備註"])));
});

test("2026-09-07 Raw 正好 4 張且未提供的量、流通、倍數全部保留缺值", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-07");

  assert.equal(raw.length, 4);
  assert.deepEqual(raw.map(row => row.Warrant_Code), ["067202", "082564", "082589", "082501"]);
  assert.deepEqual(raw.map(row => row.Underlying_Code), ["4958", "6669", "2327", "6442"]);
  assert.ok(raw.every(row => row["30m_Volume"] === null && row.Circulation === null && row.Displayed_Multiple === null));
  assert.equal(raw.find(row => row.Warrant_Code === "067202").Trade_Direction, "BUY");
  assert.equal(raw.find(row => row.Warrant_Code === "082564").Trade_Direction, "SELL");
  assert.equal(raw.find(row => row.Warrant_Code === "082589").Trade_Direction, "BUY");
  assert.equal(raw.find(row => row.Warrant_Code === "082501").Trade_Direction, "Unknown");
  assert.match(raw.find(row => row.Warrant_Code === "082564").Notes, /1\.85 倍.*Distribution/);
  assert.match(raw.find(row => row.Warrant_Code === "082501").Notes, /不因單張 Raw 自行判多/);
});

test("2026-09-07 BUY 保留 20 個名次槽與未解析 #8，SELL 完整 20 筆", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-07");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(buy.filter(row => row["母股代號"] !== null).length, 19);
  assert.deepEqual(buy.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(
    [buy[0]["母股代號"], buy[1]["母股代號"], buy[2]["母股代號"]],
    ["2409", "2454", "2330"]
  );
  assert.deepEqual(
    [buy[7]["母股代號"], buy[7]["母股名稱"], buy[7]["可見金額(萬)"], buy[7]["當日Raw"]],
    [null, null, null, null]
  );
  assert.match(buy[7]["備註"], /遮住.*保留缺值.*禁止猜測/);

  assert.equal(sell.length, 20);
  assert.deepEqual(sell.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(
    sell.slice(0, 3).map(row => [row["母股代號"], row["可見金額(萬)"]]),
    [["2454", 1781], ["3008", 1495], ["2317", 1404]]
  );
  assert.deepEqual(
    rows.filter(row => row["當日Raw"] === true).map(row => `${row["方向"]}:${row["母股代號"]}`),
    ["BUY:6669", "BUY:4958", "BUY:2327", "SELL:6669", "SELL:2327"]
  );
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
  assert.equal(data.currentObservation.length, 33);

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
  assert.equal(data.currentObservation.length, 33);
  assert.equal(data.episodes.length, 37);
  assert.ok(!data.episodes.some(row => row["進觀察日"] === "2026-09-04"));

  const auo = snapshot94.find(row => row["母股代號"] === "2409");
  const chipbond = snapshot94.find(row => row["母股代號"] === "6147");
  assert.match(auo["備註"], /Neutral follow-through.*保留 9\/3 Active Episode/);
  assert.match(chipbond["備註"], /保留既有 Episode，不新增 retrigger/);
});

test("2026-09-07 如實保存截圖 Watch 30／History 64，僅建立 29 筆可辨識觀察明細", () => {
  const snapshot = data.observationSnapshots.filter(row => row["日期"] === "2026-09-07");
  const counts = data.meta.reportedCounts["2026-09-07"];

  assert.deepEqual(
    [counts.raw, counts.activeWatch, counts.history, counts.knownWatchDetails, counts.knownEpisodeDetails],
    [4, 30, 64, 29, 33]
  );
  assert.equal(snapshot.length, 29);
  assert.equal(data.currentObservation.length, 33);
  assert.equal(new Set(snapshot.map(row => row["母股代號"])).size, 29);
  assert.ok(!snapshot.some(row => row["母股代號"] == null || row["母股名稱"] == null));
  assert.match(counts.completeness, /第 30 檔.*未解析.*不自行猜測/);

  const added = snapshot.filter(row => ["4958", "2327"].includes(row["母股代號"]));
  assert.deepEqual(added.map(row => row["母股代號"]), ["4958", "2327"]);
  assert.ok(added.every(row => row["進觀察日期"] === "2026-09-07" && row["狀態"] === "觀察中／新進"));
  assert.ok(snapshot.filter(row => ["4958", "6669", "2327", "6442"].includes(row["母股代號"])).every(row => row["當日Raw張數"] === 1));
  assert.deepEqual(
    snapshot.filter(row => row["當日Raw張數"] === 1).map(row => row["母股代號"]).sort(),
    ["2327", "4958", "6442", "6669"]
  );
});

test("2026-09-08 Watch 30／History 64 維持，沒有新增 Watch、退出或 Episode", () => {
  const previous = data.observationSnapshots.filter(row => row["日期"] === "2026-09-07");
  const snapshot = data.observationSnapshots.filter(row => row["日期"] === "2026-09-08");
  const counts = data.meta.reportedCounts["2026-09-08"];

  assert.deepEqual([counts.raw, counts.activeWatch, counts.history, counts.newWatch, counts.knownExits], [3, 30, 64, 0, 0]);
  assert.equal(snapshot.length, 29);
  assert.deepEqual(snapshot.map(row => row["母股代號"]).sort(), previous.map(row => row["母股代號"]).sort());
  assert.ok(!snapshot.some(row => ["6426", "2481"].includes(row["母股代號"])));
  assert.deepEqual(snapshot.filter(row => row["當日Raw張數"] === 1).map(row => row["母股代號"]), ["6669"]);
  assert.equal(data.currentObservation.length, 33);
  assert.equal(data.episodes.length, 37);
  assert.ok(!data.episodes.some(row => row["來源日期"] === "2026-09-08"));
  assert.match(snapshot.find(row => row["母股代號"] === "6669")["備註"], /方向反轉.*保留既有 Episode/);
  assert.match(snapshot.find(row => row["母股代號"] === "4958")["備註"], /轉弱.*未獲人工退出確認/);
  assert.match(snapshot.find(row => row["母股代號"] === "6147")["備註"], /惡化.*不自動退出/);
});

test("2026-09-09 Watch 32／History 64，僅新增南亞科與群聯", () => {
  const previous = data.observationSnapshots.filter(row => row["日期"] === "2026-09-08");
  const snapshot = data.observationSnapshots.filter(row => row["日期"] === "2026-09-09");
  const counts = data.meta.reportedCounts["2026-09-09"];
  const addedCodes = ["2408", "8299"];

  assert.deepEqual(
    [counts.raw, counts.activeWatch, counts.history, counts.newWatch, counts.knownExits, counts.knownWatchDetails, counts.knownEpisodeDetails],
    [7, 32, 64, 2, 0, 31, 35]
  );
  assert.equal(previous.length, 29);
  assert.equal(snapshot.length, 31);
  assert.equal(data.currentObservation.length, 33);
  assert.deepEqual(
    snapshot.filter(row => addedCodes.includes(row["母股代號"])).map(row => row["母股代號"]),
    addedCodes
  );
  assert.ok(snapshot.filter(row => addedCodes.includes(row["母股代號"])).every(row => row["進觀察日期"] === "2026-09-09" && row["狀態"] === "觀察中／新進"));
  assert.deepEqual(
    snapshot.filter(row => row["當日Raw張數"] > 0).map(row => [row["母股代號"], row["當日Raw張數"]]),
    [["6147", 1], ["2408", 3], ["8299", 1]]
  );
  assert.ok(!snapshot.some(row => ["2301", "3006"].includes(row["母股代號"])));
  assert.deepEqual(
    data.currentObservation.filter(row => addedCodes.includes(row["母股代號"])).map(row => row["母股代號"]),
    addedCodes
  );
});

test("南亞科與群聯各建立一筆 2026-09-09 Active Episode，舊 Episode 不變", () => {
  const additions = data.episodes.filter(row => row["來源日期"] === "2026-09-09");

  assert.equal(data.episodes.length, 37);
  assert.equal(data.episodes.filter(row => row["來源日期"] === "2026-09-09").length, 2);
  assert.deepEqual(additions.map(row => row["母股代號"]), ["2408", "8299"]);
  assert.ok(additions.every(row => row["進觀察日"] === "2026-09-09" && row["目前狀態"] === "Active"));
  assert.ok(additions.every(row => row["退出日"] === null && row["進場參考價"] === null && row["退出參考價"] === null && row["歷史報酬%"] === null));
});

test("2026-09-10 回補聖暉*後 Watch 33／History 64，並建立 Active Episode", () => {
  const previous = data.observationSnapshots.filter(row => row["日期"] === "2026-09-09");
  const snapshot = data.observationSnapshots.filter(row => row["日期"] === "2026-09-10");
  const counts = data.meta.reportedCounts["2026-09-10"];

  assert.deepEqual(
    [counts.raw, counts.activeWatch, counts.history, counts.newWatch, counts.knownExits, counts.knownWatchDetails, counts.knownEpisodeDetails],
    [6, 33, 64, 1, 0, 32, 36]
  );
  assert.equal(previous.length, 31);
  assert.equal(snapshot.length, 32);
  assert.equal(new Set(snapshot.map(row => row["母股代號"])).size, 32);
  assert.deepEqual(
    snapshot.filter(row => !previous.some(oldRow => oldRow["母股代號"] === row["母股代號"])).map(row => row["母股代號"]),
    ["5536"]
  );
  assert.deepEqual(
    snapshot.filter(row => row["當日Raw張數"] > 0).map(row => row["母股代號"]).sort(),
    ["1303", "3211", "5536", "6669"]
  );
  assert.ok(!snapshot.some(row => ["2357", "2454"].includes(row["母股代號"])));
  const saint = snapshot.find(row => row["母股代號"] === "5536");
  assert.deepEqual([saint["進觀察日期"], saint["狀態"], saint["當日Raw張數"]], ["2026-09-10", "觀察中／新進", 1]);
  assert.equal(data.currentObservation.length, 33);
  assert.equal(data.episodes.length, 37);
  const additions = data.episodes.filter(row => row["來源日期"] === "2026-09-10");
  assert.deepEqual(additions.map(row => row["母股代號"]), ["5536"]);
  assert.ok(additions.every(row => row["目前狀態"] === "Active" && row["進場參考價"] === null && row["歷史報酬%"] === null));
  assert.match(snapshot.find(row => row["母股代號"] === "3211")["備註"], /Mature|既有 Watch \/ Episode/);
  assert.match(snapshot.find(row => row["母股代號"] === "2408")["備註"], /Signal|明顯降溫|保留 9\/9 Watch \/ Episode/);
});

test("2026-09-11 Watch 34／History 64，僅新增穩懋與一筆 Active Episode", () => {
  const previous = data.observationSnapshots.filter(row => row["日期"] === "2026-09-10");
  const snapshot = data.observationSnapshots.filter(row => row["日期"] === "2026-09-11");
  const counts = data.meta.reportedCounts["2026-09-11"];

  assert.deepEqual(
    [counts.raw, counts.activeWatch, counts.history, counts.newWatch, counts.knownExits, counts.knownWatchDetails, counts.knownEpisodeDetails],
    [4, 34, 64, 1, 0, 33, 37]
  );
  assert.equal(previous.length, 32);
  assert.equal(snapshot.length, 33);
  assert.equal(new Set(snapshot.map(row => row["母股代號"])).size, 33);
  assert.deepEqual(
    snapshot.filter(row => !previous.some(oldRow => oldRow["母股代號"] === row["母股代號"])).map(row => row["母股代號"]),
    ["3105"]
  );
  assert.deepEqual(snapshot.filter(row => row["當日Raw張數"] > 0).map(row => row["母股代號"]), ["3105"]);
  assert.ok(!snapshot.some(row => ["6830", "3374", "3624"].includes(row["母股代號"])));
  const win = snapshot.find(row => row["母股代號"] === "3105");
  assert.deepEqual([win["進觀察日期"], win["狀態"], win["當日Raw張數"]], ["2026-09-11", "觀察中／新進", 1]);
  assert.deepEqual(data.currentObservation.filter(row => row["今日Raw張數"] > 0).map(row => row["母股代號"]), ["3105"]);
  const additions = data.episodes.filter(row => row["來源日期"] === "2026-09-11");
  assert.deepEqual(additions.map(row => row["母股代號"]), ["3105"]);
  assert.ok(additions.every(row => row["目前狀態"] === "Active" && row["退出日"] === null && row["進場參考價"] === null && row["歷史報酬%"] === null));
});

test("臻鼎-KY 與國巨各新增一筆 2026-09-07 Active Episode，舊 Episode 不變", () => {
  const additions = data.episodes.filter(row => row["進觀察日"] === "2026-09-07");

  assert.deepEqual(additions.map(row => row["母股代號"]), ["4958", "2327"]);
  assert.ok(additions.every(row => row["目前狀態"] === "Active"));
  assert.ok(additions.every(row => row["進場參考價"] === null && row["退出參考價"] === null && row["歷史報酬%"] === null));
  assert.equal(data.episodes.filter(row => !["2026-09-07", "2026-09-09", "2026-09-10", "2026-09-11"].includes(row["來源日期"])).length, 31);
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
  assert.doesNotMatch(source, /user_id|starting_capital|cash_balance|pending_settlement|net_asset_value|realized_pnl|unrealized_pnl|transaction_tax/i);
});
