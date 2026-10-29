# 現股當沖做空第一輪凍結規格

版本：CASH_SHORT_INTRADAY_V01_FROZEN_20261029。這是獨立離線研究，不是正式參數變更。
SHADOW_ONLY、NO_LIVE_ORDER；actual_orders=0、broker_connections=0、market_subscriptions=0。
本規格及 config.json 在計算本輪績效之前寫入，freeze.json 記錄檔案 SHA 與時間。修正程式缺陷必須留修正紀錄，不可依績效改門檻。

## 基準與資料

以目前 checkout 純 `yuanta_live_runtime_v01.strategy` 的 LONG 訊號與固定 HARD_3500_PLUS_MFE_V1_NO_LOSS_RECOVERY 出場為基準。
目前背景 paper 為 LONG_ONLY、每場 190000 元、最多一筆、最多一倉，舊成交為立即全額不利一檔代理。本研究保留相同訊號，但另列更嚴格暖機與延遲五檔容量情境；不是舊 paper 帳戶的無差別複製。
舊 direction_follow_backtest.SPEC 的5000停損、trailing與loss_recovery不是目前出場，不予混用。
`short_side_study_v01` 已有歷史 SHORT 研究和單位稽核；它不是本輪新開發的即時功能，舊容量/非法零股結果不作有效基準。
`short_swing_shadow_v01` 是日線短波段，完全排除。9/24 TODO 未找到可確認的現存原檔，不將對話補寫成程式證據。

只讀 collector 原始 ticks/books、當日 run manifest/watchlist、既有 seal/ledger，不呼叫 broker、SDK、Keychain、網路或排程。
同日多run選開始不晚於09:00且涵蓋最長正常時段的canonical run；不得串接不同seal，也不能按績效選run。
使用當日開盤前建立的原始 Top30，不用今日名單回填過去；seal/候選產生時間不符者不納入主要重播。
資料到達順序依 received_at、ingest sequence；交易所時間僅供窗口/品質檢查，不可以未收到事件推進狀態。
清盤 SerialNo=-1 與 InOutFlag10..15 為參考/狀態，不作成交；第一版 flow 只用0內盤/1外盤，定價與未知方向不硬歸類。
原始 DealVol/五檔Vol 單位若未證，保留 UNIT_UNVERIFIED。單筆量的欄位解讀也保留文件證據限制，不因數字大小猜單位。

資料分級：完整重播要求所有關鍵欄位/開盤覆蓋/歷史資格/容量單位有證；僅有tick/book但缺資格、單位或book交易所時間為 SIMPLIFIED_CONDITIONAL；缺開盤、封存損壞或候選point-in-time失敗為 NOT_EVALUABLE。
STRICT_EXECUTABLE 組對關鍵未知維持績效 NA，不把全部被安全閘擋下當策略零報酬。容量1000/1倍率只是假設情境，不能驗證單位。

## 時間切分與選擇

依日完整分組：訓練2026/09/24–10/16、驗證10/19–10/22、回溯時間留出10/27–10/29。
9/22–23晚啟動只稽核；10/23先前存在交易日曆衝突，只稽核，不拿後來行情更改當時結果。沒有封存行情的日期只列日曆/紀錄狀態。
留出期間的LONG損益在本研究前已被檢視，故不是完全未觸碰樣本外；真正未來驗證保留10/30起，狀態PENDING。沒有把日期隨機拆成逐筆樣本。
模型選擇僅看訓練+驗證：至少10筆完整交易、5個不同日，先要求合計淨損益正，再依最小回撤、最高淨利、字典序選。
不足門檻不選贏家；只用事前固定 S1_NONE 作 B/C/D 的 REFERENCE_NOT_SELECTED，第二階段不優選出場。這個門檻是避免無資料硬選，不是統計顯著性保證。
不做大規模搜尋、不年化、不宣布正期望，不以同日高度相關訊號冒充獨立樣本。小樣本不給誤導的信賴區間。

## 訊號、暖機與不追空

決策每30秒，最早09:05、最後13:10。每股首次有效成交須<=09:00:05，收到的連續暖機須滿300秒；觀察間隔>60秒或暫停後須重新暖機。
每次須有60秒至少10筆、tick<=5秒、book<=10秒、spread<=40bps；300秒大單門檻沿用90百分位、60秒方向沿用現有公式。
09:05不是自動解鎖；缺開盤資料不能重建完整VWAP/OR，不能以晚啟動的第一筆冒充開盤價。
Stage A只提供池，READY/WATCH/OVERHEATED不作空方訊號。反彈/回測狀態只由已收到資料形成，不能用收盤後高低點。

- M0：現有SHORT鏡像（flow<=-0.35、大單<=-0.25、低於VWAP、book imbalance<=-0.10、跌破前180–30秒低點），沿用原防追價。僅診斷，不作選擇候選。
- S1：先低於VWAP；反彈至VWAP附近，容許 max(2tick,0.15%價格)；形成回測高低點。至少30秒、至多120秒後跌破已形成回測低點1tick且仍低於VWAP才觸發。高於VWAP0.15%失效，setup總壽命300秒。
- S2：先完整觀察09:00–09:05 OR低點，跌破1tick後等反彈至OR低點附近（同容許距離），等待30–120秒，未站回且再破已知回測低點1tick才觸發；setup總壽命300秒。
- S1/S2共用賣方flow<=-0.35、大單<=-0.25；五檔不平衡只診斷，不額外硬篩。離回測參考位置>0.5%、結構高點停損距離>1%放棄。

S1/S2各測NONE/WEAK。WEAK固定為0050低於當時可得VWAP且300秒報酬<=0；資料缺失標UNAVAILABLE，不以日線替代。
2330、加權指數、相對0050只診斷，有資料才列；Top30廣度只稱候選池廣度，不稱全市場。
同股每日最多一次空方進場，後續合格訊號保留並標重複。策略層所有方向合計最多一筆/一倉，未成交IOC不占成交次數，但待委託期間占用額度。

## 出場與部位

E0沿用純strategy固定淨3500停損及MFE_V1鎖利（1R起鎖0、1.5R保留60%、2R70%、3R75%）、兩次反向確認；13:20開始回補。
R_price為純engine由含成本3500淨停損反解的每股初始風險；net_R=淨損益/3500另列，不混同MFE的price_R。
先用E0比較進場。第二階段只在選定進場上比較：E1額外突破進場前已形成反彈高點1tick出場；E2持有>=300秒、net_R<0.25且買方flow>0.10、大單>0時退出。既有5分鐘保護僅提供假設起點，不複用舊最佳門檻作新證據。
若未選出進場版本，E1/E2仍交付預先固定S1_NONE的診斷列，不作贏家選擇；留出只評估E0，E1/E2留出標NOT_RUN_SELECTION_INSUFFICIENT。
整張1000股，最大名目本金<=min(190000,當時權益)，不把SHORT賣出款當額外可用資金。S1/S2依結構距離與成本再限制每筆風險<=3500；一張超限即跳過。
不宣稱3500為保證最大虧損，跳空/延遲/深度不足可能超過；未完成回補保留曝險，不允許新單。

## 成交與成本假設

主情境 latency=1秒，較差情境3秒+多一tick；無實測signal-to-order分布，不以callback傳輸延遲冒充委託延遲。
只用延遲後第一份新五檔（到達後3秒內），以對手價掃最多五檔的10%容量，每檔向下取整到1000股。
做空SELL吃bid、回補BUY吃ask；進場limit固定訊號時對手價外2tick，掃價加不利1tick後仍不得越limit。無queue資料不模擬被動排隊成交。
進場IOC未填殘量在研究模型立即取消（不是券商取消證據）；cover每次新book重試，13:25前停止，未填殘量不消失。同一book容量不重複利用。
未取得合法新book不以成交價補平，缺book exchange時間/歷史上下限都列執行限制。13:25後集合競價不以連續撮合模型填單。
費率先沿用程式各邊0.000855、最低20元向上取整；股票現股當沖賣出稅0.0015。實際帳戶折扣/券差費用未確認，不假裝實際對帳；未完成當沖另列一般稅率差額估算與不可估券差/強制買回成本。
成交筆數是研究模擬，不是actual fills。嚴格組未能確認資格與容量則NA，與假設組分開。

## 組合與交付

A=LONG-only，同一執行模型；B=選定SHORT-only；C=LONG+SHORT共用190000/每日1筆/3500風險；D=每一當下決策無合格LONG才允許SHORT。
同時訊號先到達時間，再score降序，同分LONG優先，再股票碼升序。D不是盤後看到整天無多單才回補空單。
所有合格訊號診斷與受限帳戶績效分開。空方占用後錯過的多方候選保留；不將每訊號都成交結果視為組合收益。
每日估值使用已收到的對手可用報價；不可估者保留最後可用值、stale標記與未平風險，完整績效另設incomplete旗標。
交付DATA_AUDIT、experiments、trades、signals、daily_results、execution_issues、grouped_diagnostics、事件片段、manifest、測試及重播入口。缺值NA/PENDING不填0。最佳一天扣除為集中度診斷，不拿來回頭選股。

官方規則（本輪只讀參考，不回填歷史資格）：
- https://www.twse.com.tw/zh/products/system/day-trading.html
- https://www.twse.com.tw/zh/trading/day-trading.html
- https://www.twse.com.tw/zh/products/system/trading.html
- https://www.etax.nat.gov.tw/etwmain/tax-info/understanding/tax-knowledge/rwG2M1N
