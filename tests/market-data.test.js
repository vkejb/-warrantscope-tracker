const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

global.window = {};
require("../data.js");
const data = global.window.WS_DATA;

test("2026-09-08 是最新日期，9/4 No-Trigger Day 仍完整保留", () => {
  assert.equal(data.meta.defaultDate, "2026-09-08");
  assert.equal(data.meta.dates.at(-1), "2026-09-08");
  assert.equal(data.raw.filter(row => row.Date === "2026-09-04").length, 0);
  assert.match(data.meta.notes["2026-09-04"], /No-Trigger Day.*Raw 0 張.*0 檔母股.*0 issuer/);
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
  assert.equal(data.currentObservation.length, 29);

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
  assert.equal(data.currentObservation.length, 29);
  assert.equal(data.episodes.length, 33);
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
  assert.equal(data.currentObservation.length, 29);
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
  assert.deepEqual(data.currentObservation.filter(row => row["今日Raw張數"] === 1).map(row => row["母股代號"]), ["6669"]);
  assert.equal(data.episodes.length, 33);
  assert.ok(!data.episodes.some(row => row["來源日期"] === "2026-09-08"));
  assert.match(snapshot.find(row => row["母股代號"] === "6669")["備註"], /方向反轉.*保留既有 Episode/);
  assert.match(snapshot.find(row => row["母股代號"] === "4958")["備註"], /轉弱.*未獲人工退出確認/);
  assert.match(snapshot.find(row => row["母股代號"] === "6147")["備註"], /惡化.*不自動退出/);
});

test("臻鼎-KY 與國巨各新增一筆 2026-09-07 Active Episode，舊 Episode 不變", () => {
  const additions = data.episodes.filter(row => row["進觀察日"] === "2026-09-07");

  assert.deepEqual(additions.map(row => row["母股代號"]), ["4958", "2327"]);
  assert.ok(additions.every(row => row["目前狀態"] === "Active"));
  assert.ok(additions.every(row => row["進場參考價"] === null && row["退出參考價"] === null && row["歷史報酬%"] === null));
  assert.equal(data.episodes.filter(row => row["來源日期"] !== "2026-09-07").length, 31);
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
