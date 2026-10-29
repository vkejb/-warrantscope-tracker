# 現股當沖 SHORT 離線研究 V0.1

SHADOW_ONLY / NO_LIVE_ORDER / actual_orders=0。不匯入券商adapter、SDK或正式runtime main；不登入、不訂閱、不送單、不修改既有排程/正式參數。

這是新隔離研究，不是 `short_swing_shadow_v01` 日線短波段，也不將 `short_side_study_v01` 的未證單位/非法零股舊績效當成基準。

## 重現

需要本repository、Python>=3.10、原封存collector行情/當日Top30及seal。原始行情、帳戶、freeze/results不發布至公開GitHub。

從 `stock-strategy` 執行：

```sh
python3 -B -m cash_short_intraday_research_v01.freeze
python3 -B -m unittest discover -s cash_short_intraday_research_v01/tests -v
python3 -u -B -m cash_short_intraday_research_v01.run --output cash_short_intraday_research_v01/results_reproduction
python3 -B -m cash_short_intraday_research_v01.report cash_short_intraday_research_v01/results_reproduction
```

freeze只在本地第一次建立，存在即拒絕覆寫。研究output必須全新目錄；不覆寫任何既有結果。交付包包含原本地freeze，可對照manifest及來源hash。若要在新工作目錄重現，傳`--root`指向持有同一hash原封存的checkout；不要以新資料默默代替。

`run`先產生tables.json等機器可讀結果；CSV以Artifact Tool匯出（Codex隨附 `@oai/artifact-tool`）：

```sh
node cash_short_intraday_research_v01/export_tables.mjs cash_short_intraday_research_v01/results_reproduction
python3 -B -m cash_short_intraday_research_v01.deliver cash_short_intraday_research_v01/results_reproduction
```

CSV匯出需要Artifact Tool套件可解析；不含套件的外部環境仍能用標準庫讀取tables.json，或將各陣列以標準CSV writer匯出。不要為重現而登入券商或下載今天行情。

## 規格與限制

config.json、EXPERIMENT_SPEC.md在績效前凍結；細節及缺陷修正另見IMPLEMENTATION_NOTES.md。少量固定M0/S1/S2＋NONE/WEAK，不做大量參數搜尋；每個當下使用已收到資料。

STRICT_EXECUTABLE對缺單位/歷史資格保持NA。條件情境的1/1000倍容量假設不能驗證量單位，也不構成可實現報酬。費率沿用現碼而非帳戶對帳。真正未來驗證另列PENDING。

完整交易/費用/曝險逐筆列出。未回補不當作平倉、不能從樣本刪掉。沒有完成交易不報勝率。原始背景paper的立即全額成交與本研究延遲容量模型分開。

## 檔案

- data.py：原始來源/時點/hash與逐股覆蓋。
- signals.py：只在已收到資料上推進暖機、flow及反彈/回測狀態。
- execution.py：延遲後單次五檔、整張部分成交與成本情境。
- replay.py / run.py：共用資金、當下仲裁、訓練/驗證選擇及時間留出。
- report.py / export_tables.mjs / deliver.py：本機報告、CSV核對及白名單壓縮包，沒有上傳功能。
- tests：合成資料因果、單位、費稅、延遲、深度守恆、回補、無實單依賴等regressions。

公開版只包含研究原碼/設定/文件/合成測試。不包含真實結果、原始行情、帳戶資料、原本地freeze、log或delivery ZIP。
