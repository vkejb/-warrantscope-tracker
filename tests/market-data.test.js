const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

global.window = {};
require("../data.js");
const data = global.window.WS_DATA;

test("2026-09-22 V1 是最新交易日，9/19 不建成交易日", () => {
  assert.equal(data.meta.defaultDate, "2026-09-22");
  assert.equal(data.meta.dates.at(-1), "2026-09-22");
  assert.ok(!data.meta.dates.includes("2026-09-19"));
  assert.equal(data.raw.filter(row => row.Date === "2026-09-19").length, 0);
  assert.equal(data.raw.filter(row => row.Date === "2026-09-04").length, 0);
  assert.match(data.meta.notes["2026-09-04"], /No-Trigger Day.*Raw 0 張.*0 檔母股.*0 issuer/);
});

test("2026-09-22 Raw 3 張，代號、券商、方向與缺值原樣保留", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-22");

  assert.equal(raw.length, 3);
  assert.deepEqual(raw.map(row => row.Warrant_Code), ["712572", "086941", "083776"]);
  assert.deepEqual(raw.map(row => row.Underlying_Code), ["3105", "2059", "3035"]);
  assert.deepEqual(raw.map(row => row.Issuer), ["國泰", "凱基", "群益"]);
  assert.deepEqual(raw.map(row => row.Trade_Direction), ["Unknown", "BUY-confirmed", "BUY-confirmed"]);
  assert.ok(raw.every(row => (
    row["30m_Volume"] === null && row.Circulation === null && row.Displayed_Multiple === null
    && row.Time_LastSeen === null && row.Prior_Buy_Rank === null && row.Prior_Sell_Rank === null
  )));
  assert.match(raw.find(row => row.Underlying_Code === "3105").Notes, /方向尚未確認.*不因 Raw 自動視為 bullish/);
  assert.ok(raw.filter(row => ["2059", "3035"].includes(row.Underlying_Code)).every(row => /Signal 不等於 Entry/.test(row.Notes)));
});

test("2026-09-22 BUY／SELL 各 20 筆，分點、評級、加總與 Raw 交叉正確", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-22");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["1815", 1279], ["4958", 1222], ["2059", 1196], ["6223", 847], ["6706", 761],
    ["6147", 736], ["2449", 693], ["2327", 685], ["3035", 684], ["0050", 609],
    ["2308", 608], ["6488", 545], ["2408", 514], ["3211", 506], ["3665", 486],
    ["2360", 473], ["2303", 468], ["3265", 458], ["2368", 448], ["3016", 447]
  ]);
  assert.deepEqual(sell.map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["2301", 1971], ["2454", 1478], ["2308", 1320], ["3324", 1235], ["3016", 1207],
    ["3665", 1006], ["2330", 822], ["2449", 802], ["5274", 779], ["6147", 751],
    ["6538", 726], ["2368", 723], ["6683", 718], ["0050", 697], ["6239", 682],
    ["2303", 663], ["1802", 662], ["3406", 630], ["6290", 611], ["2409", 606]
  ]);
  assert.ok(rows.every(row => (
    row["分點1"] && row["分點2"] && row["分點1評級"] && row["分點2評級"]
    && row["分點1可見金額(萬)"] + row["分點2可見金額(萬)"] === row["可見金額(萬)"]
    && row["As_Of"] === "2026-09-22 V1" && row["資料完整度"] === "V1_CONFIRMED"
  )));
  assert.deepEqual(rows.filter(row => row["當日Raw"]).map(row => `${row["方向"]}:${row["母股代號"]}`), [
    "BUY:2059", "BUY:3035"
  ]);
});

test("2026-09-22 新增川湖與智原，Watch 32／History 77 且沒有退出", () => {
  const counts = data.meta.reportedCounts["2026-09-22"];
  const previous = data.observationSnapshots.filter(row => row["日期"] === "2026-09-21");
  const snapshot = data.observationSnapshots.filter(row => row["日期"] === "2026-09-22");
  const additions = ["2059", "3035"];

  assert.deepEqual(
    [counts.raw, counts.rawUnderlyings, counts.activeWatch, counts.history, counts.newWatch, counts.knownExits,
      counts.knownWatchDetails, counts.knownEpisodeDetails],
    [3, 3, 32, 77, 2, 0, 8, 48]
  );
  assert.equal(previous.length, 6);
  assert.equal(snapshot.length, 8);
  assert.ok(previous.every(row => snapshot.some(item => item["母股代號"] === row["母股代號"])));
  assert.deepEqual(snapshot.filter(row => additions.includes(row["母股代號"])).map(row => row["母股代號"]), additions);
  assert.deepEqual(snapshot.filter(row => row["當日Raw張數"] > 0).map(row => row["母股代號"]), additions);
  assert.ok(!snapshot.some(row => row["母股代號"] === "3105"));
  assert.equal(data.episodes.length, 48);
  assert.deepEqual(
    data.episodes.filter(row => row["進觀察日"] === "2026-09-22").map(row => row["母股代號"]),
    additions
  );
  assert.ok(data.episodes.filter(row => row["進觀察日"] === "2026-09-22").every(row => (
    row["目前狀態"] === "Active" && row["退出日"] === null && row["進場參考價"] === null
    && row["退出參考價"] === null && row["歷史報酬%"] === null
  )));
});

test("2026-09-21 Raw 8 張／6 檔母股，代號、券商與缺值原樣保留", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-21");

  assert.equal(raw.length, 8);
  assert.equal(new Set(raw.map(row => row.Underlying_Code)).size, 6);
  assert.deepEqual(raw.map(row => row.Warrant_Code), [
    "081969", "086086", "077591", "082838", "086436", "712291", "712185", "713225"
  ]);
  assert.deepEqual(raw.map(row => row.Underlying_Code), [
    "3605", "2360", "2301", "2360", "2409", "3324", "3260", "3260"
  ]);
  assert.deepEqual(raw.map(row => row.Issuer), ["群益", "凱基", "台新", "群益", "台新", "台新", "元大", "國票"]);
  assert.ok(raw.every(row => (
    row["30m_Volume"] === null && row.Circulation === null && row.Displayed_Multiple === null
    && row.Time_LastSeen === null && row.Prior_Buy_Rank === null && row.Prior_Sell_Rank === null
  )));
  assert.ok(raw.filter(row => ["2360", "3260"].includes(row.Underlying_Code)).every(row => /兩張 Raw/.test(row.Notes)));
  assert.match(raw.find(row => row.Underlying_Code === "3324").Notes, /SELL #3.*偏空/);
  assert.ok(raw.filter(row => !["2301", "2360", "3324"].includes(row.Underlying_Code)).every(row => /不自動加入觀察/.test(row.Notes)));
  assert.ok(raw.filter(row => ["2301", "2360", "3324"].includes(row.Underlying_Code)).every(row => /回補確認.*進觀察.*Active Episode/.test(row.Notes)));
});

test("2026-09-21 BUY／SELL 各 20 筆，分點加總、方向與 Raw 交叉正確", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-21");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["2449", 1554], ["2454", 1159], ["2330", 1111], ["1815", 1006], ["3017", 961],
    ["3016", 921], ["2404", 909], ["3661", 882], ["2368", 802], ["3008", 797],
    ["6706", 697], ["7769", 684], ["2303", 636], ["5289", 556], ["2408", 537],
    ["6139", 535], ["6147", 532], ["3711", 529], ["6488", 522], ["2345", 516]
  ]);
  assert.deepEqual(sell.map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["2330", 1111], ["2615", 1109], ["3324", 883], ["2308", 829], ["2313", 763],
    ["2395", 760], ["2303", 710], ["5347", 609], ["1303", 528], ["2408", 523],
    ["1560", 509], ["3231", 499], ["2376", 490], ["3675", 466], ["6147", 463],
    ["1326", 450], ["6257", 412], ["3665", 378], ["2603", 371], ["6620", 367]
  ]);
  assert.ok(rows.every(row => (
    row["分點1"] === null && row["分點2"] === null
    && row["分點1可見金額(萬)"] + row["分點2可見金額(萬)"] === row["可見金額(萬)"]
    && row["As_Of"] === "2026-09-21 V1" && row["資料完整度"] === "V1_CONFIRMED"
  )));
  assert.deepEqual(rows.filter(row => row["當日Raw"]).map(row => `${row["方向"]}:${row["母股代號"]}`), ["SELL:3324"]);
});

test("2026-09-21 回補光寶科、致茂、雙鴻，Watch 30／History 77", () => {
  const counts = data.meta.reportedCounts["2026-09-21"];
  const previous = data.observationSnapshots.filter(row => row["日期"] === "2026-09-18");
  const snapshot = data.observationSnapshots.filter(row => row["日期"] === "2026-09-21");
  const additions = ["2301", "2360", "3324"];

  assert.deepEqual(
    [counts.raw, counts.rawUnderlyings, counts.activeWatch, counts.history, counts.newWatch, counts.knownExits,
      counts.knownWatchDetails, counts.knownEpisodeDetails],
    [8, 6, 30, 77, 3, 0, 6, 46]
  );
  assert.equal(previous.length, 3);
  assert.equal(snapshot.length, 6);
  assert.ok(previous.every(row => snapshot.some(item => item["母股代號"] === row["母股代號"])));
  assert.deepEqual(snapshot.filter(row => additions.includes(row["母股代號"])).map(row => row["母股代號"]), additions);
  assert.deepEqual(snapshot.filter(row => additions.includes(row["母股代號"])).map(row => row["當日Raw張數"]), [1, 2, 1]);
  assert.deepEqual(
    data.episodes.filter(row => row["進觀察日"] === "2026-09-21").map(row => row["母股代號"]),
    additions
  );
  assert.ok(data.episodes.filter(row => row["進觀察日"] === "2026-09-21").every(row => (
    row["目前狀態"] === "Active" && row["退出日"] === null && row["進場參考價"] === null
    && row["退出參考價"] === null && row["歷史報酬%"] === null
  )));
});

test("2026-09-18 Raw 5 張，方向、備註與未提供數值原樣保留", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-18");

  assert.equal(raw.length, 5);
  assert.equal(new Set(raw.map(row => row.Underlying_Code)).size, 5);
  assert.deepEqual(raw.map(row => row.Warrant_Code), ["711816", "703396", "086428", "077601", "079242"]);
  assert.deepEqual(raw.map(row => row.Trade_Direction), [
    "Mixed/slight BUY", "Unknown", "Mixed/slight SELL", "BUY-confirmed", "BUY-confirmed"
  ]);
  assert.ok(raw.every(row => (
    row["30m_Volume"] === null && row.Circulation === null && row.Displayed_Multiple === null
    && row.Time_LastSeen === null && row.Prior_Buy_Rank === null && row.Prior_Sell_Rank === null
  )));
  assert.match(raw.find(row => row.Underlying_Code === "6147").Notes, /9\/15移入歷史.*未重新加入觀察/);
  assert.match(raw.find(row => row.Underlying_Code === "5274").Notes, /未加入觀察/);
});

test("2026-09-18 CLOSE BUY／SELL 各 20 筆，兩個可見分點金額與 Raw 標記正確", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-18");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(sell.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(buy.map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["2449", 1554], ["2454", 1159], ["2330", 1111], ["1815", 1006], ["3017", 961],
    ["3016", 921], ["2404", 909], ["3661", 882], ["2368", 802], ["3008", 797],
    ["6706", 697], ["7769", 684], ["2303", 636], ["5289", 556], ["2408", 537],
    ["6139", 535], ["6147", 532], ["3711", 529], ["6488", 522], ["2345", 516]
  ]);
  assert.deepEqual(sell.map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["2330", 1111], ["2615", 1109], ["3324", 883], ["2308", 829], ["2313", 763],
    ["2395", 760], ["2303", 710], ["5347", 609], ["1303", 528], ["2408", 523],
    ["1560", 509], ["3231", 499], ["2376", 490], ["3675", 466], ["6147", 463],
    ["1326", 450], ["6257", 412], ["3665", 378], ["2603", 371], ["6620", 367]
  ]);
  assert.ok(rows.every(row => (
    row["分點1"] === null && row["分點2"] === null
    && row["分點1可見金額(萬)"] + row["分點2可見金額(萬)"] === row["可見金額(萬)"]
    && row["As_Of"] === "CLOSE" && row["資料完整度"] === "CLOSE_CONFIRMED"
  )));
  assert.deepEqual(rows.filter(row => row["當日Raw"]).map(row => `${row["方向"]}:${row["母股代號"]}`), [
    "BUY:3017", "BUY:2368", "BUY:2303", "BUY:6147", "SELL:2303", "SELL:6147"
  ]);
  assert.ok(rows.every(row => /近似值.*不代表完整主力淨額.*依紅／綠字原樣收錄/.test(row["備註"])));
});

test("2026-09-18 Watch 27／History 77 保留計數，未知退出不捏造明細", () => {
  const counts = data.meta.reportedCounts["2026-09-18"];
  const snapshot = data.observationSnapshots.filter(row => row["日期"] === "2026-09-18");
  const previous = data.observationSnapshots.filter(row => row["日期"] === "2026-09-17");
  const newCodes = ["2303", "2368", "3017"];

  assert.deepEqual(
    [counts.raw, counts.rawUnderlyings, counts.activeWatch, counts.history, counts.newWatch,
      counts.reportedExits, counts.knownExits, counts.unidentifiedExits,
      counts.knownWatchDetails, counts.knownEpisodeDetails],
    [5, 5, 27, 77, 3, 13, 2, 11, 3, 43]
  );
  assert.equal(counts.sourceRefreshedAt, "2026-09-19 12:48 Asia/Taipei");
  assert.match(counts.completeness, /頎邦.*矛盾.*未據此改寫舊日期/);
  assert.equal(previous.length, 36);
  assert.deepEqual(snapshot.map(row => row["母股代號"]), newCodes);
  assert.ok(newCodes.every(code => data.currentObservation.some(row => row["母股代號"] === code)));
  assert.ok(snapshot.every(row => row["進觀察日期"] === "2026-09-18" && row["當日Raw張數"] === 1));
  assert.ok(!data.currentObservation.some(row => ["6147", "5274"].includes(row["母股代號"])));

  assert.equal(data.episodes.length, 48);
  const newEpisodes = data.episodes.filter(row => row["來源日期"] === "2026-09-18");
  assert.deepEqual(newEpisodes.map(row => row["母股代號"]), newCodes);
  assert.ok(newEpisodes.every(row => row["目前狀態"] === "Active" && row["退出日"] === null
    && row["進場參考價"] === null && row["退出參考價"] === null && row["歷史報酬%"] === null));
  for (const [code, start, returnPct] of [["2395", "2026-09-15", 0.7], ["2313", "2026-09-17", 4.6]]) {
    const episode = data.episodes.find(row => row["母股代號"] === code && row["進觀察日"] === start);
    assert.deepEqual([episode["目前狀態"], episode["退出日"], episode["歷史報酬%"]], ["Exited", "2026-09-18", returnPct]);
  }
  assert.equal(data.episodes.filter(row => row["目前狀態"] === "Unresolved").length, 34);
  assert.ok(!data.episodes.some(row => row["來源日期"] === "2026-09-19"));
});

test("2026-09-17 V3 Raw 5 張／4 檔母股，方向與缺值原樣保留", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-17");

  assert.equal(raw.length, 5);
  assert.equal(new Set(raw.map(row => row.Underlying_Code)).size, 4);
  assert.deepEqual(raw.map(row => row.Warrant_Code), ["074448", "046961", "714011", "079532", "052771"]);
  assert.deepEqual(raw.map(row => row.Underlying_Code), ["2313", "1303", "5536", "2489", "1303"]);
  assert.deepEqual(raw.map(row => row.Issuer), ["群益", "中信", "凱基", "台新", "統一"]);
  assert.ok(raw.every(row => row.Trade_Direction === "BUY-confirmed"));
  assert.ok(raw.every(row => (
    row["30m_Volume"] === null && row.Circulation === null && row.Displayed_Multiple === null
    && row.Time_LastSeen === null && row.Episode_Type === null
    && row.Prior_Buy_Rank === null && row.Prior_Sell_Rank === null && row.Notes === null
  )));
  assert.equal(raw.filter(row => row.Underlying_Code === "1303").length, 2);
});

test("2026-09-17 V3 BUY／SELL 各 20 筆，排行、金額與 Raw 交叉標記正確", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-17");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(sell.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(buy.map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["2489", 883], ["2313", 802], ["2409", 743], ["2330", 743], ["2308", 671],
    ["5269", 666], ["3231", 624], ["6488", 617], ["5347", 613], ["2303", 570],
    ["2615", 550], ["5536", 545], ["6770", 506], ["3675", 488], ["3105", 457],
    ["0050", 439], ["1326", 426], ["3665", 426], ["1303", 408], ["6693", 396]
  ]);
  assert.deepEqual(sell.map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["6669", 1716], ["3324", 1414], ["6683", 1134], ["3406", 1055], ["4958", 979],
    ["6213", 975], ["1815", 928], ["6147", 824], ["3260", 812], ["3037", 742],
    ["2454", 718], ["2344", 647], ["2408", 596], ["3008", 585], ["5269", 546],
    ["2308", 538], ["3017", 538], ["2368", 529], ["2409", 516], ["6442", 517]
  ]);
  assert.deepEqual(rows.filter(row => row["當日Raw"]).map(row => `${row["方向"]}:${row["母股代號"]}`), [
    "BUY:2489", "BUY:2313", "BUY:5536", "BUY:1303"
  ]);
  assert.ok(rows.every(row => (
    row["分點1"] === null && row["分點1可見金額(萬)"] === null
    && row["分點2"] === null && row["分點2可見金額(萬)"] === null
  )));
  assert.ok(rows.every(row => /V3 更新包提供的近似值.*不代表完整主力淨額.*依來源方向原樣收錄/.test(row["備註"])));
});

test("2026-09-16 Raw 12 張／7 檔母股，Cross-Warrant 與缺值原樣保留", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-16");

  assert.equal(raw.length, 12);
  assert.equal(new Set(raw.map(row => row.Underlying_Code)).size, 7);
  assert.deepEqual(raw.map(row => row.Warrant_Code), [
    "065434", "089540", "052771", "061670", "707324", "067596",
    "065252", "065895", "061577", "047792", "054711", "048870"
  ]);
  assert.deepEqual(
    ["2454", "1303", "2059"].map(code => [code, raw.filter(row => row.Underlying_Code === code).length]),
    [["2454", 3], ["1303", 3], ["2059", 2]]
  );
  assert.ok(raw.every(row => row["30m_Volume"] === null && row.Circulation === null && row.Displayed_Multiple === null));
  assert.ok(raw.every(row => row.Time_LastSeen === null && row.Notes === null));
  assert.ok(raw.filter(row => row.Underlying_Code === "2454").every(row => (
    row.Trade_Direction === "Mixed" && row.Prior_Buy_Rank === 8 && row.Prior_Sell_Rank === 4
  )));
  assert.ok(raw.filter(row => row.Underlying_Code === "2408").every(row => (
    row.Trade_Direction === "SELL-lean / Two-way" && row.Prior_Buy_Rank === 20 && row.Prior_Sell_Rank === 6
  )));
});

test("2026-09-16 BUY／SELL 各 20 筆，排行、金額與 Raw 交叉標記正確", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-16");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(sell.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(buy.map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["2353", 3054], ["3324", 1707], ["2409", 1571], ["6669", 1146], ["2301", 995],
    ["6213", 854], ["3260", 777], ["2454", 711], ["2330", 689], ["6147", 653],
    ["2344", 618], ["3105", 565], ["3016", 532], ["3406", 531], ["2303", 530],
    ["6223", 499], ["5269", 492], ["3017", 476], ["3006", 470], ["2408", 465]
  ]);
  assert.deepEqual(sell.map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["6669", 1072], ["4958", 835], ["2395", 671], ["2454", 666], ["2049", 659],
    ["2408", 652], ["2303", 582], ["6139", 516], ["3665", 492], ["3624", 462],
    ["6223", 455], ["3189", 432], ["3406", 425], ["0050", 421], ["6451", 409],
    ["3042", 389], ["6488", 356], ["6147", 317], ["2308", 317], ["6196", 309]
  ]);
  assert.deepEqual(rows.filter(row => row["當日Raw"]).map(row => `${row["方向"]}:${row["母股代號"]}`), [
    "BUY:2454", "BUY:2408", "SELL:2454", "SELL:2408"
  ]);
  assert.ok(rows.every(row => (
    row["分點1"] === null && row["分點1可見金額(萬)"] === null
    && row["分點2"] === null && row["分點2可見金額(萬)"] === null
  )));
  assert.ok(rows.every(row => /近似值.*不代表完整主力淨額.*依來源方向原樣收錄/.test(row["備註"])));
});

test("2026-09-15 Raw 5 張／4 檔母股，南亞兩張 Cross-Warrant 原樣保留", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-15");

  assert.equal(raw.length, 5);
  assert.equal(new Set(raw.map(row => row.Underlying_Code)).size, 4);
  assert.deepEqual(raw.map(row => row.Warrant_Code), ["074455", "053810", "076856", "052771", "052673"]);
  assert.deepEqual(raw.map(row => row.Underlying_Code), ["2395", "1303", "6669", "1303", "3189"]);
  assert.deepEqual(raw.map(row => row.Issuer), ["群益", "兆豐", "永豐", "統一", "統一"]);
  assert.deepEqual(raw.map(row => row.Trade_Direction), ["BUY", "SELL", "BUY", "SELL", "Unknown"]);
  assert.ok(raw.every(row => row["30m_Volume"] === null && row.Circulation === null && row.Displayed_Multiple === null));
  assert.ok(raw.filter(row => row.Underlying_Code === "1303").every(row => row.Episode_Type === "Cross-Warrant / Sell-side Raw" && row.Prior_Sell_Rank === 14));
  assert.match(raw.find(row => row.Warrant_Code === "052673").Notes, /方向尚未確認/);
});

test("2026-09-15 BUY／SELL 各 20 筆，金額、分點與 Raw 交叉標記正確", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-15");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(sell.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(buy.map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["6669", 1529], ["2301", 1396], ["3324", 982], ["2395", 731], ["2049", 615],
    ["3008", 612], ["3406", 533], ["3653", 528], ["6223", 466], ["0050", 445],
    ["4958", 443], ["2464", 426], ["3042", 366], ["2454", 365], ["3105", 358],
    ["2308", 345], ["2404", 341], ["6811", 326], ["3624", 322], ["2408", 311]
  ]);
  assert.deepEqual(sell.map(row => [row["母股代號"], row["可見金額(萬)"]]), [
    ["6147", 3045], ["3324", 2659], ["3016", 1993], ["2368", 1383], ["2308", 1207],
    ["3443", 1053], ["2454", 826], ["3260", 782], ["3105", 748], ["2303", 684],
    ["2324", 569], ["6770", 467], ["6505", 461], ["1303", 455], ["2345", 439],
    ["3711", 426], ["1802", 424], ["2327", 422], ["2305", 421], ["3533", 420]
  ]);
  assert.ok(rows.every(row => row["分點1可見金額(萬)"] + row["分點2可見金額(萬)"] === row["可見金額(萬)"]));
  assert.deepEqual(rows.filter(row => row["當日Raw"]).map(row => `${row["方向"]}:${row["母股代號"]}`), [
    "BUY:6669", "BUY:2395", "SELL:1303"
  ]);
  assert.ok(rows.every(row => /兩個分點加總近似值.*依頁面方向原樣收錄/.test(row["備註"])));
});

test("2026-09-14 Raw 正好 2 張，缺值與偏多方向完整保留", () => {
  const raw = data.raw.filter(row => row.Date === "2026-09-14");

  assert.equal(raw.length, 2);
  assert.deepEqual(raw.map(row => row.Warrant_Code), ["713246", "071061"]);
  assert.deepEqual(raw.map(row => row.Underlying_Code), ["3260", "2454"]);
  assert.deepEqual(raw.map(row => row.Issuer), ["凱基", "永豐"]);
  assert.ok(raw.every(row => row["30m_Volume"] === null && row.Circulation === null && row.Displayed_Multiple === null));
  assert.ok(raw.every(row => row.Trade_Direction === "BUY"));
  assert.deepEqual(raw.map(row => [row.Prior_Buy_Rank, row.Prior_Sell_Rank]), [[3, null], [4, null]]);
  assert.match(raw.find(row => row.Warrant_Code === "071061").Notes, /9\/10 Raw \+ SELL #1.*方向明顯反轉/);
});

test("2026-09-14 BUY 與 SELL Top20 完整，分點加總與 Raw 標記正確", () => {
  const rows = data.mainforce.filter(row => row["日期"] === "2026-09-14");
  const buy = rows.filter(row => row["方向"] === "BUY");
  const sell = rows.filter(row => row["方向"] === "SELL");

  assert.equal(buy.length, 20);
  assert.equal(sell.length, 20);
  assert.deepEqual(buy.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(sell.map(row => row["排名"]), Array.from({length: 20}, (_, index) => index + 1));
  assert.deepEqual(
    buy.map(row => [row["母股代號"], row["可見金額(萬)"]]),
    [
      ["1815", 746], ["3711", 741], ["3260", 661], ["2454", 644], ["2345", 619],
      ["3665", 601], ["6770", 590], ["2308", 589], ["7769", 531], ["3034", 503],
      ["5269", 487], ["3211", 482], ["3042", 426], ["3016", 409], ["5483", 408],
      ["6805", 406], ["2303", 397], ["3661", 394], ["2449", 362], ["2344", 354]
    ]
  );
  assert.deepEqual(
    sell.map(row => [row["母股代號"], row["可見金額(萬)"]]),
    [
      ["2409", 3558], ["1303", 1846], ["6669", 916], ["3406", 855], ["2330", 671],
      ["6531", 553], ["2360", 549], ["3231", 446], ["3008", 440], ["2303", 393],
      ["3017", 390], ["2376", 365], ["4967", 351], ["6209", 336], ["2368", 325],
      ["2354", 319], ["2351", 316], ["4958", 306], ["6830", 296], ["5386", 270]
    ]
  );
  assert.ok(rows.every(row => row["分點1可見金額(萬)"] + row["分點2可見金額(萬)"] === row["可見金額(萬)"]));
  assert.deepEqual(
    rows.filter(row => row["當日Raw"] === true).map(row => `${row["方向"]}:${row["母股代號"]}`),
    ["BUY:3260", "BUY:2454"]
  );
  assert.ok(rows.every(row => /兩個分點加總近似值.*方向依頁面側別收錄，不反轉/.test(row["備註"])));
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
  assert.equal(data.currentObservation.length, 8);

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
  assert.equal(data.currentObservation.length, 8);
  assert.equal(data.episodes.length, 48);
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
  assert.equal(data.currentObservation.length, 8);
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
  assert.equal(data.currentObservation.length, 8);
  assert.equal(data.episodes.length, 48);
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
  assert.equal(data.currentObservation.length, 8);
  assert.deepEqual(snapshot.filter(row => addedCodes.includes(row["母股代號"])).map(row => row["母股代號"]), addedCodes);
  assert.ok(snapshot.filter(row => addedCodes.includes(row["母股代號"])).every(row => row["進觀察日期"] === "2026-09-09" && row["狀態"] === "觀察中／新進"));
  assert.deepEqual(
    snapshot.filter(row => row["當日Raw張數"] > 0).map(row => [row["母股代號"], row["當日Raw張數"]]),
    [["6147", 1], ["2408", 3], ["8299", 1]]
  );
  assert.ok(!snapshot.some(row => ["2301", "3006"].includes(row["母股代號"])));
});

test("南亞科與群聯的 2026-09-09 Episode 保留，9/18 現況待核對", () => {
  const additions = data.episodes.filter(row => row["來源日期"] === "2026-09-09");

  assert.equal(data.episodes.length, 48);
  assert.equal(data.episodes.filter(row => row["來源日期"] === "2026-09-09").length, 2);
  assert.deepEqual(additions.map(row => row["母股代號"]), ["2408", "8299"]);
  assert.ok(additions.every(row => row["進觀察日"] === "2026-09-09" && row["目前狀態"] === "Unresolved"));
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
  assert.equal(data.currentObservation.length, 8);
  assert.equal(data.episodes.length, 48);
  const additions = data.episodes.filter(row => row["來源日期"] === "2026-09-10");
  assert.deepEqual(additions.map(row => row["母股代號"]), ["5536"]);
  assert.ok(additions.every(row => row["目前狀態"] === "Unresolved" && row["進場參考價"] === null && row["歷史報酬%"] === null));
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
  const additions = data.episodes.filter(row => row["來源日期"] === "2026-09-11");
  assert.deepEqual(additions.map(row => row["母股代號"]), ["3105"]);
  assert.ok(additions.every(row => row["目前狀態"] === "Unresolved" && row["退出日"] === null && row["進場參考價"] === null && row["歷史報酬%"] === null));
});

test("2026-09-14 Watch 34／History 64 維持，沒有新增 Watch、退出或 Episode", () => {
  const previous = data.observationSnapshots.filter(row => row["日期"] === "2026-09-11");
  const snapshot = data.observationSnapshots.filter(row => row["日期"] === "2026-09-14");
  const counts = data.meta.reportedCounts["2026-09-14"];

  assert.deepEqual(
    [counts.raw, counts.activeWatch, counts.history, counts.newWatch, counts.knownExits, counts.knownWatchDetails, counts.knownEpisodeDetails],
    [2, 34, 64, 0, 0, 33, 37]
  );
  assert.equal(previous.length, 33);
  assert.equal(snapshot.length, 33);
  assert.deepEqual(snapshot.map(row => row["母股代號"]).sort(), previous.map(row => row["母股代號"]).sort());
  assert.deepEqual(snapshot.filter(row => row["當日Raw張數"] > 0).map(row => row["母股代號"]), ["3260"]);
  assert.ok(!snapshot.some(row => row["母股代號"] === "2454"));
  assert.equal(data.currentObservation.length, 8);
  assert.equal(data.episodes.length, 48);
  assert.ok(!data.episodes.some(row => row["來源日期"] === "2026-09-14"));
  assert.match(snapshot.find(row => row["母股代號"] === "3260")["備註"], /Raw \+ BUY #3.*不新增 Episode/);
  assert.match(snapshot.find(row => row["母股代號"] === "2409")["備註"], /SELL #1.*元大-向上.*元大-西屯/);
  assert.match(snapshot.find(row => row["母股代號"] === "4958")["備註"], /SELL #18/);
  assert.ok(snapshot.filter(row => ["2408", "6147"].includes(row["母股代號"])).every(row => /BUY \/ SELL 均未進 Top20/.test(row["備註"])));
});

test("2026-09-15 回補研華進觀察，Watch 35／History 64 與 Active Episode 正確", () => {
  const previous = data.observationSnapshots.filter(row => row["日期"] === "2026-09-14");
  const snapshot = data.observationSnapshots.filter(row => row["日期"] === "2026-09-15");
  const counts = data.meta.reportedCounts["2026-09-15"];

  assert.deepEqual(
    [counts.raw, counts.rawUnderlyings, counts.activeWatch, counts.history, counts.newWatch, counts.knownExits, counts.knownWatchDetails, counts.knownEpisodeDetails],
    [5, 4, 35, 64, 1, 0, 34, 38]
  );
  assert.equal(previous.length, 33);
  assert.equal(snapshot.length, 34);
  assert.ok(previous.every(row => snapshot.some(item => item["母股代號"] === row["母股代號"])));
  assert.equal(snapshot.filter(row => row["母股代號"] === "2395").length, 1);
  assert.deepEqual(snapshot.filter(row => row["當日Raw張數"] > 0).map(row => [row["母股代號"], row["當日Raw張數"]]).sort(), [
    ["1303", 2], ["2395", 1], ["6669", 1]
  ]);
  assert.ok(!snapshot.some(row => row["母股代號"] === "3189"));
  assert.equal(data.currentObservation.length, 8);
  assert.equal(data.episodes.length, 48);
  const advantechEpisode = data.episodes.find(row => row["母股代號"] === "2395" && row["進觀察日"] === "2026-09-15");
  assert.deepEqual(
    [advantechEpisode["目前狀態"], advantechEpisode["進場參考價"], advantechEpisode["退出參考價"], advantechEpisode["歷史報酬%"]],
    ["Exited", null, null, 0.7]
  );
  assert.match(snapshot.find(row => row["母股代號"] === "2395")["備註"], /Raw \+ BUY #4.*回補確認/);
  assert.match(snapshot.find(row => row["母股代號"] === "6669")["備註"], /Raw \+ BUY #1.*多方 cross/);
  assert.match(snapshot.find(row => row["母股代號"] === "1303")["備註"], /兩張 Raw.*偏空 Cross-Warrant/);
  assert.match(snapshot.find(row => row["母股代號"] === "6147")["備註"], /SELL #1.*divergence.*後續確認/);
  assert.match(snapshot.find(row => row["母股代號"] === "2408")["備註"], /BUY #20.*輕度改善/);
  assert.match(snapshot.find(row => row["母股代號"] === "2409")["備註"], /均未進 Top20.*尚無買方回補確認/);
});

test("2026-09-16 Watch 35／History 64 延續，不因 Raw 自動新增觀察", () => {
  const previous = data.observationSnapshots.filter(row => row["日期"] === "2026-09-15");
  const snapshot = data.observationSnapshots.filter(row => row["日期"] === "2026-09-16");
  const counts = data.meta.reportedCounts["2026-09-16"];

  assert.deepEqual(
    [counts.raw, counts.rawUnderlyings, counts.activeWatch, counts.history, counts.newWatch, counts.knownExits, counts.knownWatchDetails, counts.knownEpisodeDetails],
    [12, 7, 35, 64, 0, 0, 34, 38]
  );
  assert.equal(previous.length, 34);
  assert.equal(snapshot.length, 34);
  assert.deepEqual(snapshot.map(row => row["母股代號"]).sort(), previous.map(row => row["母股代號"]).sort());
  assert.deepEqual(snapshot.filter(row => row["當日Raw張數"] > 0).map(row => [row["母股代號"], row["當日Raw張數"]]).sort(), [
    ["1303", 3], ["2408", 1], ["3008", 1]
  ]);
  assert.equal(data.currentObservation.length, 8);
  assert.equal(data.episodes.length, 48);
  assert.ok(!snapshot.some(row => ["2454", "2059", "2317", "3374"].includes(row["母股代號"])));
  assert.equal(data.episodes.filter(row => row["來源日期"] === "2026-09-16").length, 1);
  assert.match(snapshot.find(row => row["母股代號"] === "2395")["備註"], /SELL #3.*follow-through deterioration/);
  assert.match(snapshot.find(row => row["母股代號"] === "2408")["備註"], /Raw \+ BUY #20／SELL #6.*賣方可見量較強/);
  assert.match(snapshot.find(row => row["母股代號"] === "1303")["備註"], /Raw x3.*均未進 Top20/);
});

test("2026-09-17 V3 新增華通與瑞軒，Watch 37／History 64 與 Episode 正確", () => {
  const previous = data.observationSnapshots.filter(row => row["日期"] === "2026-09-16");
  const snapshot = data.observationSnapshots.filter(row => row["日期"] === "2026-09-17");
  const counts = data.meta.reportedCounts["2026-09-17"];
  const additions = ["2313", "2489"];

  assert.deepEqual(
    [counts.raw, counts.rawUnderlyings, counts.activeWatch, counts.history, counts.newWatch, counts.knownExits, counts.knownWatchDetails, counts.knownEpisodeDetails],
    [5, 4, 37, 64, 2, 0, 36, 40]
  );
  assert.equal(previous.length, 34);
  assert.equal(snapshot.length, 36);
  assert.ok(previous.every(row => snapshot.some(item => item["母股代號"] === row["母股代號"])));
  assert.deepEqual(snapshot.filter(row => additions.includes(row["母股代號"])).map(row => row["母股代號"]), additions);
  assert.deepEqual(snapshot.filter(row => row["當日Raw張數"] > 0).map(row => [row["母股代號"], row["當日Raw張數"]]).sort(), [
    ["1303", 2], ["2313", 1], ["2489", 1], ["5536", 1]
  ]);
  assert.equal(data.currentObservation.length, 8);
  assert.equal(data.episodes.length, 48);
  const newEpisodes = data.episodes.filter(row => row["來源日期"] === "2026-09-17");
  assert.deepEqual(newEpisodes.map(row => row["母股代號"]), additions);
  assert.deepEqual(newEpisodes.map(row => row["目前狀態"]), ["Exited", "Unresolved"]);
  assert.equal(newEpisodes[0]["歷史報酬%"], 4.6);
  assert.ok(newEpisodes.every(row => row["進場參考價"] === null && row["退出參考價"] === null));
  assert.match(snapshot.find(row => row["母股代號"] === "2489")["備註"], /Raw \+ BUY #1.*列入觀察/);
  assert.match(snapshot.find(row => row["母股代號"] === "2313")["備註"], /Raw \+ BUY #2.*列入觀察/);
  assert.match(snapshot.find(row => row["母股代號"] === "5536")["備註"], /Raw \+ BUY #12.*follow-through/);
  assert.match(snapshot.find(row => row["母股代號"] === "1303")["備註"], /Raw x2 \+ BUY #19.*Cross-Warrant/);
  assert.match(snapshot.find(row => row["母股代號"] === "2409")["備註"], /BUY #3.*SELL #19.*買方較強/);
  assert.match(counts.completeness, /V3 正式取代同日 V1／V2/);
});

test("臻鼎-KY 與國巨的 2026-09-07 Episode 保留，9/18 現況待核對", () => {
  const additions = data.episodes.filter(row => row["進觀察日"] === "2026-09-07");

  assert.deepEqual(additions.map(row => row["母股代號"]), ["4958", "2327"]);
  assert.ok(additions.every(row => row["目前狀態"] === "Unresolved"));
  assert.ok(additions.every(row => row["進場參考價"] === null && row["退出參考價"] === null && row["歷史報酬%"] === null));
  assert.equal(data.episodes.filter(row => !["2026-09-07", "2026-09-09", "2026-09-10", "2026-09-11"].includes(row["來源日期"])).length, 42);
});

test("友達保留舊 Episode 並建立 9/3 新 Episode", () => {
  const auoEpisodes = data.episodes.filter(row => row["母股代號"] === "2409");
  assert.equal(auoEpisodes.length, 2);

  const historical = auoEpisodes.find(row => row["目前狀態"] === "Exited");
  assert.deepEqual(
    [historical["進觀察日"], historical["退出日"], historical["歷史報酬%"]],
    ["2026-08-12", "2026-08-20", -1.7]
  );

  const unresolved = auoEpisodes.find(row => row["目前狀態"] === "Unresolved");
  assert.equal(unresolved["進觀察日"], "2026-09-03");
  assert.equal(unresolved["退出日"], null);

  const wiwynn = data.episodes.find(row => row["母股代號"] === "6669" && row["目前狀態"] === "Unresolved");
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
