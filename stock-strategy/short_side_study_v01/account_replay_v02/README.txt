LONG A + raw SHORT 帳戶回放：合法整股容量修正版

只新增本隔離目錄，不更改LIVE、舊s4/replay/execution/policy/statistics或既有輸出。
保留同一6日cohort、訊號、SHORT thresholds、資金/費稅與exit。
時間排序先完整approved佔用當日唯一機會；同boundary才以原score排序。
SHORT不增加0050 hard gate。regime只描述，不尋找參數。

由現有s4 runner回放全部6日×3策略×3execution情境×2單位假設=108列。
唯一成交模型差異是每一價位必須以1000股整倍數成交。
保留原下單目標，不改用零股，也不讓數量在跨整張邊界時虛構replenishment。
合法部分成交仍保留；未回補/缺證據維持null，不拿完成子集當完整帳戶績效。
進場未成交餘量立即撤除是沿用研究模型假設，不是券商受理/撤單認證。
UNIT_UNVERIFIED與ELIGIBILITY_UNPROVEN保留。所有績效是條件假設，不是實際單位結論。

閱讀results/report_zh-TW.txt、summary.json、counterfactual_replacements.json與daily_results.json。
baseline_parity.json證明1000假設54列與舊結果相同。
quantity_parity.json證明兩假設已選到訊號的請求量一致。
provenance.json與validation_manifest.json列明來源、原碼、舊輸出與新輸出hash。

重現（stock-strategy工作目錄；不需任何憑證或broker登入）：
PYTHONDONTWRITEBYTECODE=1 python3 -m short_side_study_v01.account_replay_v02.run
PYTHONDONTWRITEBYTECODE=1 python3 -m short_side_study_v01.account_replay_v02.verify

run必須完成S4_REPLAY_COMPLETE後才能verify，不可驗證部分執行結果。
純離線程序注入研究成交backend，patch於呼叫後還原；不載入/建立SDK物件。
舊results_s4與results_expanded禁止作輸出目錄。無push/merge/部署或LIVE啟用。
