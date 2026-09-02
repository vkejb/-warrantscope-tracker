const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const portfolioSource = fs.readFileSync(path.join(root, "portfolio.js"), "utf8");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const publicData = fs.readFileSync(path.join(root, "data.js"), "utf8");

test("六張私人資料表都由登入後的 Supabase client 讀取", () => {
  for (const table of ["transactions", "price_snapshots", "user_settings", "cash_flows", "daily_snapshots", "trade_episodes"]) {
    assert.match(portfolioSource, new RegExp(`client\\.from\\(\\"${table}\\"\\)`));
  }
  assert.match(portfolioSource, /if \(!session\?\.user\)/);
  assert.match(portfolioSource, /await loadPrivateData\(session, message\)/);
  assert.match(portfolioSource, /position_market_value,position_liquidation_value,net_asset_value/);
});

test("損益頁包含帳戶、績效、持倉、現金流、交易、Episode 與資產曲線區塊", () => {
  for (const id of [
    "account-overview-grid",
    "performance-grid",
    "position-list",
    "cash-flow-list",
    "transaction-list",
    "closed-episode-list",
    "equity-curve",
  ]) {
    assert.match(html, new RegExp(`id=\\"${id}\\"`));
  }
});

test("公開資料檔不包含 Supabase 私人資料設定或秘密金鑰", () => {
  assert.doesNotMatch(publicData, /dwnwilahkbbdvhsdiifd|sb_publishable_|service_role|sb_secret_/i);
  assert.doesNotMatch(portfolioSource, /service_role|sb_secret_/i);
});
