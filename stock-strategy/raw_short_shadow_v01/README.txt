raw SHORT S1 紙上觀察接線與回補安全 mock

範圍：新增本目錄，不修改正常LONG、OrderQty、adapter、正式回報/DB/風控。
UNIT_UNVERIFIED只針對行情五檔量與研究容量，並不代表現有LONG下單有問題。
本模組沒有SDK實例、broker adapter、SHORT真單routing，也不讀實際baseline。

既有部分重用：
- 原收盤paper_shadow_v01是LONG-only，auto_runner已接該版本，但沒有raw S1/S4 hook。
- short_side_study_v01.s4.chronological_selection：S0/S1獨立confirmation與最早approved S4。
- account_replay_v02：原hard stop/MFE/13:20退出與合法整股容量，兩种五檔單位假設。
- intraday_paper_execution_v01.PaperExecutionEngine：既有SQLite/WAL/FULL同步、去重、部分成交、待撤與重啟。

新增接線（手動單次、收盤後因果紙上回放，不是即時真單）：
既有COMPLETE行情封存 → 原訊號 → 紙上進場 → 原持倉/MFE管理 → 回補 → 獨立S0/S1/S4日報。
S1只記錄0050，資料缺少時regime=UNKNOWN，不新增SHORT gate。S0原有LONG A規則不變。
三帳戶與兩單位假設各自保存，不相加或事後挑方向。每帳戶一天最多一筆；連續boundary合併episode，
episode不是交易樣本。重跑同一日/來源不增加樣本，不同同日來源記DATA_CONFLICT而不選較賺結果。

baseline是mock用合成常數，不讀真實帳戶庫存。回補只取已成交剩餘空單，先扣未終結BUY保留量。
待撤確認不能單獨释放保留量，須完整MOCK_ONLY訂單/库存快照確認最終累計成交；拒單/UNKNOWN/
取消失敗/缺賣盤/過期ask不能標FLAT。崩潰重啟必須重新mock對帳，人工先回補要記錄mock外部成交。
既有未回補紙上部位跨日保留並阻擋該帳戶/單位假設新進場，不在新一天重設成零。
跨日未完成紀錄保留null；本輪不新增跨日稅費、借券成本模型或自動券商救援routing。

資格：每一天保存全部30檔的當沖標的與暫停先賣後買證據/UNKNOWN、來源日期/hash。
TWSE可使用exact-date既有cache或--fetch-public單次公開HTTP；失敗保存原因與UNKNOWN，不反覆重試。
TPEx未有exact-day來源保持UNKNOWN；帳戶權限、券源、額度一律UNKNOWN，不是取得下單授權。
五檔原始封存不更動，raw_entry_book.json保留原字串；容量損益只是1000/1股每單位條件假設。

命令全部於stock-strategy執行：

唯讀既有行情重跑（不動activation、prospective日帳、正式DB；只寫新隔離驗收輸出）：
PYTHONDONTWRITEBYTECODE=1 python3 -m raw_short_shadow_v01.main replay --source yuanta_intraday_shadow_v01/runtime/runs/20261013T005009.746979Z_cca032b2 --output raw_short_shadow_v01/validation/replay-check-01
再次重跑請換新的replay-check名稱；既有輸出禁止覆寫。此命令永遠是ENGINEERING_ACCEPTANCE_NOT_PROSPECTIVE。

讀取紙上接線狀態：
PYTHONDONTWRITEBYTECODE=1 python3 -m raw_short_shadow_v01.main status

已凍結/啟用後，手動處理新完成的既有行情封存（不登入、不送單、不啟停行情程序）：
PYTHONDONTWRITEBYTECODE=1 python3 -m raw_short_shadow_v01.main run-day --source yuanta_intraday_shadow_v01/runtime/runs/實際新run_id --fetch-public
或一次掃描既有新COMPLETE封存（不輪詢、不等待）：
PYTHONDONTWRITEBYTECODE=1 python3 -m raw_short_shadow_v01.main drain --fetch-public

10/14起首次prospective驗收（唯讀檢查，不處理或改寫封存/日帳）：
PYTHONDONTWRITEBYTECODE=1 python3 -m raw_short_shadow_v01.prospective_acceptance
WAITING_FOR_FIRST_QUALIFIED_COMPLETE_DAY表示尚無符合完整時段、零回呼錯誤及30檔有效tick/book的封存；
READY_FOR_MANUAL_DRAIN表示已有新封存但尚未手動處理，資料是否合格須待驗收器逐檔核對。
處理後重新執行驗收，分帳顯示SIGNAL、NO_SIGNAL或DATA_MISSING，逐一核對原始訊號/五檔、
兩種五檔單位假設下重新計算進出場，唯讀查核mock SQLite成交、剩餘持倉、回補、去重及來源品質。NEEDS_REVIEW或
ACCEPTANCE_FAILED不得當成通過；UNIT_UNVERIFIED和模擬成交未經券商驗證的標記保留。

紙上activation.json記實際UTC凍結/手動啟用時間及程式hash，無倒填時間參數。
只有source.started_at >= enabled_at的完整新封存才可加入prospective；今天已收完的資料只做工程驗收。
二十筆是各紙上帳戶/單位假設獨立重新檢視節點，不是合計六組結果、不包括連續訊號重複筆數，
不會自動開LIVE。未成交、未回補、缺資料與衝突日均保留。

排程目前未安裝，也未改collector hook：參閱schedule_plan_zh-TW.txt。新排程不需broker或Keychain。
範例plist只是交付方案，本輪沒有copy到Library/LaunchAgents，沒有launchctl/啟停程序。

交付validation/：工程驗收、S0/S1/S4獨立日報、cover safety mock證據、測試log、hash清單，
activation_receipt.json與prospective_status.json為真實啟用收據/零新樣本狀態。
不要把工程驗收損益當prospective新樣本。紙上SQLite與runtime由本目錄gitignore排除。
