# 做空研究證據補強與帳戶對帳

獨立於第一、二輪的只讀證據／算術對帳與離線驗證入口。SHADOW_ONLY、NO_LIVE_ORDER、actual_orders=0；不改正式LONG、Stage A、adapter、PROD、排程或原結果。GitHub只公開研究程式，results、freeze、receipt、快照、原始證據及ZIP私下保存。

## 原版隔離與對帳

從repository內stock-strategy執行，所有output均須全新路徑。isolation恢復實際原碼／輸入本體並核對原freeze，找不到matching body就失敗；不把hash當成已還原環境。target須在目前repository以外，不覆蓋任何工作樹。

```text
python3 -B -m cash_short_intraday_research_v01.evidence_followup_v01.isolation --snapshot /path/to/new_snapshot.json
python3 -B -m cash_short_intraday_research_v01.evidence_followup_v01.isolation --target /path/to/fresh-isolated-copy --receipt /path/to/new_isolation_receipt.json
python3 -B -m cash_short_intraday_research_v01.evidence_followup_v01.account_audit --isolated-root /path/to/pinned-copy --round2-results cash_short_intraday_research_v01/round2/results_round2_v1 --output /path/to/fresh-account-audit
```

隔離副本需要原本機封存資料及第一輪結果。ZIP附研究依賴原碼本體、原freeze與來源hash，但不附完整raw feed、SDK二進位或帳戶DB；不宣稱歷史SDK程序／OS／網路環境完整復原。

## 證據核對

`evidence_audit --help`與`eligibility_audit --help`列完整CLI。預設不連網；只有明示`--fetch-official`／`--acquire-public-evidence`才取得公開官方文件，不是行情訂閱。已保存receipt可離線重核對，不以後來HTTP修訂內容冒充原始取得證據。

```text
python3 -B -m cash_short_intraday_research_v01.evidence_followup_v01.eligibility_audit --receipts-from /path/to/private/eligibility_tables.json --output /path/to/fresh/eligibility_tables.json
```

費用既有帳本查核僅限明示提供的專案LaunchAgent，必須加`--fee-launchagent /path/to/private.plist`；不登入、不讀Keychain/.env、不匯出帳號或任意payload。DB存在或欄位名稱可映射不代表實際費用證據；純設定／paper估算與實際帳務分開。來源／SDK欄位證據及正規化sidecar保留原值、單位、semantic與basis，unknown不自動換算。

## 後續固定驗證

見FORWARD_SPEC.md與forward_config.json。只保留R0/R2/R5＋E0及空手；不降低原採用門檻，不做新條件／新參數搜尋。`forward --freeze`拒絕覆寫，`--check`只驗證資料，`--evaluate`才執行離線原版研究worker。兩者都重驗完整profile與隔離原碼本體，不能偷用現在改版LONG。

沒有新日期則PENDING，不啟新排程。每次evaluate是一個新190000元cohort；同一manifest內跨日equity及剩倉carry，但未實作跨batch resume。不可把分批結果併稱連續paper帳戶。新日期manifest應在未看結果前預先劃定完整連續日期範圍，缺資料整批列明並阻擋，不當無訊號或零損益。

## 展示／驗證／交付

assemble只整合已完成JSON證據／對帳，不計新交易；export只產CSV。Node匯出依賴相容的`@oai/artifact-tool`執行環境；Python原碼／JSON對帳不依賴它。下列交付命令需要本機同版private audits/profile/materials，public checkout本身沒有私有資料：

```text
python3 -B -m cash_short_intraday_research_v01.evidence_followup_v01.assemble --output /path/to/completed-followup --isolated-root /path/to/pinned-copy
node cash_short_intraday_research_v01/evidence_followup_v01/export_tables.mjs /path/to/completed-followup
python3 -B -m cash_short_intraday_research_v01.evidence_followup_v01.deliver --output /path/to/completed-followup --isolated-root /path/to/pinned-copy
```

私下ZIP另需本輪code-only publication receipt及獨立交付審查，`deliver --package`是既定白名單交付器而非通用打包指令；不覆寫既有ZIP。舊結果、使用者dirty檔案、當前正式LONG及forward freeze都必須保持一致。
