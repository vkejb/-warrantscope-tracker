五檔單位與歷史 SHORT 資格稽核 v0.1

結論：EVIDENCE_INSUFFICIENT_CLOSED_NO_REPLAY / UNIT_UNVERIFIED。
本次是證據稽核，不是新策略、參數搜尋、交易 gate 或新績效回測。
依使用者的「證據不足可結案」分支交付；不需登入券商確認。

閱讀順序：
1. report_zh-TW.txt：結論、限制、逐項完成證據。
2. unit_evidence.json：市場、Flag、數量欄位與獨立單位證據矩陣。
3. results/audit.json：6日原始封存、180筆資格、108筆舊模擬稽核、舊報告清單。
4. sdk_evidence.json：靜態 DLL 固定版本、套件宣告、來源 SHA256、隨附文件摘要。
5. yuanta_inquiry_zh-TW.txt：可直接交元大查證的草稿，尚未送出。
6. results/tests.txt、results/validation_manifest.json：已執行的測試與輸出 hash。

可重現步驟（repo root；不登入、不匯入 SDK、不啟停交易程序）：

PYTHONDONTWRITEBYTECODE=1 /Users/linyunyan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 stock-strategy/short_side_study_v01/unit_eligibility_audit_v01/inspect_sdk.py --sdk /Users/linyunyan/Downloads/YuantaSparkAPI_osx-arm64_Python --output stock-strategy/short_side_study_v01/unit_eligibility_audit_v01/sdk_evidence.json

PYTHONDONTWRITEBYTECODE=1 python3 stock-strategy/short_side_study_v01/unit_eligibility_audit_v01/audit.py --output stock-strategy/short_side_study_v01/unit_eligibility_audit_v01/results

PYTHONDONTWRITEBYTECODE=1 python3 stock-strategy/short_side_study_v01/unit_eligibility_audit_v01/verify.py

依賴：一般 Python3 標準庫供 audit/verify；inspect_sdk 另需 bundled openpyxl/pypdf
及 macOS textutil，全部唯讀處理 SDK 文件。不能把 SDK DLL 載入或執行來替代本腳本。
資料路徑來自 results_expanded/data_inventory.json 中固定6日來源，不會自動擴充日期。
來源檔或 hash 改變時會停止，不會靜默換資料或刪缺資料交易。

不同電腦需保留原始封存並明確重新定位 source_directory，不可拿新的封存代替。
inspect_sdk 只確認目前安裝檔；歷史 DLL 與 collector 程式版本仍未綁定。
audit.json 是可重現的來源/舊結果稽核，不是重新計算帳戶績效。
舊 results、results_expanded、results_s4 均原封保留，不可再把其數字視為合法成交證明。

限制：沒有元大版本對應的五檔單位確認，沒有獨立可對時五檔，沒有歷史券商額度
或受理證據。TPEx 68筆無完整歷史標的/暫停資料；TWSE缺列22筆維持未知。
後續若拿到新證據，需另行批准續作；此交付不會自動重跑或操作交易環境。
