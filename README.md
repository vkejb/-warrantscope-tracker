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
- 手機版響應式排版

## 資料狀態

- 8/24～8/28：依目前可回收的資料整理；部分賣方資料仍標 Partial / Missing
- 8/31：Raw + 買方 Top20 + 賣方 Top20 已收錄
- 9/1：Raw 15 張 + 完整 23 檔觀察中已收錄；買賣排行尚待盤後補入

## 之後放到 GitHub Pages

回家後可以：

1. GitHub 建立 repository，例如 `warrantscope-tracker`
2. 把這個資料夾內的檔案全部上傳到 repository 根目錄
3. Repository → Settings → Pages
4. Source 選 `Deploy from a branch`
5. Branch 選 `main` / `(root)`
6. 儲存後等一下，就會得到固定網址

重要：GitHub Pages 預設是公開網站。這一版只建議放市場研究資料，不要放你的私人持倉、成本、資金或個資。

## 檔案說明

- `index.html`：首頁
- `style.css`：畫面樣式
- `app.js`：月份 / 日期 / 搜尋 / 顯示邏輯
- `data.js`：目前所有研究資料
- `README.md`：這份說明

之後每日更新時，主要會更新 `data.js`，網站網址本身不用改。

Codex push test
