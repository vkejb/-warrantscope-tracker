# R0／R2／R5後續固定驗證規格

僅離線、SHADOW_ONLY、NO_LIVE_ORDER、actual_orders=0。此規格不新增策略條件，不啟用SHORT、不登入或新增訂閱／排程。正式LONG已另改版，不研究新LONG與SHORT共用資金，不引用舊A/C/D當新LONG績效。

## 固定策略與來源

R0=原S1_NONE＋E0。R2=同S1＋RS300≤−0.002＋E0。R5=同S1＋0050低於當時VWAP且300秒報酬≤0＋同RS條件＋E0。其餘flow≤−0.35、大單≤−0.25、回測／轉弱狀態機、暖機、新鮮度、spread、防追空0.5%、整張、資金及風險完全沿用已凍結第一／二輪。五檔不平衡不加新硬門檻。R1/R3/R4及E1/E2舊結果完整保留，但不擴充、不交叉搜尋。

每版本各自190000元、每日一筆一倉、3500元風險預算（不是損失保證）、整1000股。BASE=1秒及不利1tick，WORSE=3秒及不利2tick，SHARES=只把原五檔量倍率從1000改1。費率仍原代理0.000855、最低20、每張模型IOC委託費用向上取整，賣稅0.0015向上取整；不是實際帳務認證。依原sizing實際公式本金加entry fee受cap約束、結構風險含模型往返費稅受3500約束；舊『額外預留一份3500及往返費』註解未由公式實現，不自行修改。

原SDK量單位、歷史資格／券商授權、五檔交易所時間、實際費率及真正委託延遲仍未充分證實。任何單項確認不使STRICT_EXECUTABLE整體升級。若將來改單位／成本／成交模型，必須新資料版本、全R0/R2/R5及空手共同重跑，不與舊假設混算。

原13日都屬已知探索資料。後續凍結檔會保存本規格、設定、驗證入口、原碼依賴及三版本hash。不能把缺原碼只有hash稱為恢復環境：isolation_receipt須列每個實際source body與hash；未恢復歷史SDK／OS／網路程序狀態另註，不假稱完全重建券商環境。

## 新日期與資料合約

僅接受Asia/Taipei日期嚴格晚於forward_freeze.frozen_at當地日期的全新封存，並有未曾查看結果的明確attestation及其證據檔hash。程式不能自行證明人類從未在別處看過，因此標UNSEEN_ATTESTED而非獨立認證。舊13日及其他凍結前日期一律拒絕，不能改名為樣本外。

輸入manifest包含schema、unseen_attestation（outcomes_inspected=false、basis、evidence_path、evidence_sha256）及runs。每run須有day、path、eligibility（可空但unknown明列）；path必須是單一已封存COMPLETE/FULL_SESSION、coverage通過、source hash與preopen Stage A seal可核對的canonical run。禁止latest、盤後選股回填、用日線替代0050盤中行情。

資格提供已知限制可排除（含來源hash、事件生效日、資料取得日）；未知不變成通過。交易所表不證明券商授權。提供未知資格只允許conditional情境，不能稱合法／strict成交。今日無限制不能反證歷史無限制。

保留每個日期，包含無訊號、虧損及資料不足。批次有不完整日則先輸出逐日拒絕原因並安全阻擋整批績效，不對未知日填0；修復資料或在明確保留排除清單的新manifest中另作有效範圍評估。0050於個別訊號不足時R2/R5=UNAVAILABLE，與false分開。共同範圍只取R0/R2/R5可評估signal交集，R0也在交集重新仲裁，不取三條件全部通過交集。

只用receipt cursor之前資訊，以原scanner生成新日期S1候選，不能拿今天名單回測舊日。排序、同一decision一次IOC、限價、延遲、新五檔、10%深度、部分成交及未完成回補沿用原碼；SHORT賣出款不擴額度。收盤強制回補觸發不是成交；未完成保留剩量與未知估值。

## 統計與判讀

並列自身及共同範圍、空手0元、相對R0淨損益與回撤、所有候選／可評估／資料拒絕／條件拒絕／委託／成交／未平數、逐筆成交及費稅、日權益與完整模型mark路徑。含未平損益回撤，未知完整損益／回撤維持NA。沒有完成交易不填勝率或PF。成本敏感性、扣最佳一天／一筆及按股票／日期集中度必報。

同一交易跨版本重複不算獨立證據。至少10筆、5交易日、淨損益正的原門檻不降低；達標不是穩健性或上線證據。保留所有失敗情境，不使用新驗證結果再調同一版本。沒有新合格日期就是PENDING，不啟新排程。

## 執行入口

使用本機已恢復原始source bodies、258pins一致的隔離副本。先對forward.py／本設定／本SPEC及原依賴凍結，再接收新日期manifest。`--check`只驗證日期、來源與attestation，不計績效；`--evaluate`才啟動同一個離線worker，worker重驗所有gates，無法跳過freeze或實單限制。所有output必須新目錄，不覆寫第一／二輪或既有驗證。

```text
cd stock-strategy
python3 -B -m cash_short_intraday_research_v01.evidence_followup_v01.forward --freeze --isolated-root /path/to/pinned-copy --freeze-file /path/to/new_forward_freeze.json
python3 -B -m cash_short_intraday_research_v01.evidence_followup_v01.forward --check --isolated-root /path/to/pinned-copy --freeze-file /path/to/new_forward_freeze.json --manifest /path/to/new_dates.json --output /path/to/fresh_check
python3 -B -m cash_short_intraday_research_v01.evidence_followup_v01.forward --evaluate --isolated-root /path/to/pinned-copy --freeze-file /path/to/new_forward_freeze.json --manifest /path/to/new_dates.json --output /path/to/fresh_validation
```

本次若沒有新封存，不執行evaluate。manifest範例只含schema，不填虛構行情／績效或資格。
