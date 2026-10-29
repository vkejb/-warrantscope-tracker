# 第二輪績效前核對與修正紀錄

本輪不依損益調參，六個使用者指定門檻與第一輪資料/成交/成本假設保持不變。

- 合成邊界檢查將成本侵蝕定義對齊gross>0且net<=0，不漏掉淨損益恰為0。
- 已知book exchange timestamp亦須在目標時點10秒內；只fresh receipt而exchange已舊的報價不得估值。真實封存book缺exchange clock，所以仍conditional，不能提升認證。
- R3及八單VWAP診斷優先同一已驗證receipt cursor，資料不fresh或無完整開盤的歷史方向維持UNKNOWN，不以非空數值硬判。
- 固定cohort與帳戶重播分開，先前未解曝險不在後日消失；全未平損益NA。FIXED_COHORT本來就不是可執行資金帳，不把強制固定的後日入場當合法新倉。
- split回撤以該split期初權益重設高點，保留日內local DD及min/max順序充分統計，且輸出去重完整模型估值路徑（不是raw行情），供獨立重算。
- 2026-10-29T14:25:23Z曾在查詢protocol --help時，因當時CLI尚無argparse而提早建立freeze；**沒有計算第二輪真實績效**。該檔保留為freeze_preliminary_no_performance_20261029T142523.json，未刪除或覆寫。加入無副作用的--help/--verify、安全CLI測試及正式freeze前fresh precheck重驗，之後才建立正式freeze及開跑。
- 原第一輪所有檔案SHA保持不變；新增程式僅round2。報告/CSV作者層與包裝程式不負責交易決策，另留作者hash，不將後續排版更動冒稱事前交易演算法。
