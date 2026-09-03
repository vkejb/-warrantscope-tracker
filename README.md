# WarrantScope Tracker v1

這是第一版靜態網站，可直接在電腦上開啟，也可以之後部署到 GitHub Pages 取得固定網址。

## 先在電腦預覽

1. 解壓縮 `WarrantScope_tracker_web_v1.zip`
2. 直接雙擊 `index.html`
3. Safari / Chrome / Edge 都可以開
4. 不需要安裝 Python、Node.js 或伺服器

## 目前功能

- 月份選單
- 日期選單 + 日期快捷按鈕
- 每日 Raw 爆量權證
- 同母股 Raw 張數 / issuer 數彙整
- 買方主力排行
- 賣方主力排行
- 觀察中快照
- Episode 資料庫
- 股票搜尋與事件時間線
- Supabase Email / Password 私人損益帳本
- 帳戶總覽：起始本金、現金、待交割、持倉淨清算價值、總資產與累計績效
- 交易新增、完整交易紀錄、目前持倉、移動加權平均成本與已結束 Trade Episode
- 現金流／帳務調節、本週／本月／累計績效與每日資產曲線
- 手機版響應式排版
- 三萬元零股策略實驗室：資金／成本參數、Episode 回測、勝率、報酬與最大回撤

## 資料狀態

- 8/24～8/28：依目前可回收的資料整理；部分賣方資料仍標 Partial / Missing
- 8/31：Raw + 買方 Top20 + 賣方 Top20 已收錄
- 9/1：Raw 15 張 + 完整 23 檔觀察中 + 買賣 Top20 已收錄
- 9/2：Raw 6 張 + 買方 Top20 + 賣方 Top20 已收錄；排行金額為兩個可見分點加總的近似值

## 之後放到 GitHub Pages

回家後可以：

1. GitHub 建立 repository，例如 `warrantscope-tracker`
2. 把這個資料夾內的檔案全部上傳到 repository 根目錄
3. Repository → Settings → Pages
4. Source 選 `Deploy from a branch`
5. Branch 選 `main` / `(root)`
6. 儲存後等一下，就會得到固定網址

重要：GitHub Pages 預設是公開網站。市場研究資料仍放在 `data.js`；私人交易、持倉與損益只透過登入後的 Supabase RLS API 讀寫，不得放進 `data.js` 或公開 JSON。前端只使用可公開的 Supabase publishable key，不得加入 service role key、secret key 或資料庫密碼。

## 檔案說明

- `index.html`：首頁
- `style.css`：畫面樣式
- `app.js`：月份 / 日期 / 搜尋 / 顯示邏輯
- `portfolio.js`：Supabase Auth、六張私人資料表讀取、交易寫入與損益介面
- `portfolio-core.js`：持倉、交割現金、外部現金流、績效與 Episode 計算
- `backtest-core.js`：零股部位配置、交易成本與 Episode 回測引擎
- `backtest-ui.js`：策略規則、回測參數與結果介面
- `data.js`：目前所有研究資料
- `tests/portfolio-core.test.js`：損益計算與超賣防護測試
- `tests/portfolio-integration.test.js`：登入邊界、私人資料表與損益介面接線測試
- `tests/market-data.test.js`：日期、Raw 與買賣 Top20 完整性測試
- `README.md`：這份說明

之後每日更新時，主要會更新 `data.js`，網站網址本身不用改。

損益與私人資料整合測試：`node --test tests/*.test.js`

Codex push test
