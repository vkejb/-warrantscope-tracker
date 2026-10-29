# 現股當沖做空第二輪：條件與出場診斷凍結規格

SHADOW_ONLY、NO_LIVE_ORDER、actual_orders=0。不修改第一輪、正式LONG、Stage A、adapter、PROD與排程；不登入、不訂閱、不送單。這不是必須找到獲利版本的搜尋。

## 證據、切分與不變條件

先核對results_round1_v2、第一輪SPEC/config、9份原碼、來源manifest及檔案SHA。第一輪完全保留；第二輪獨立config/SPEC/freeze與全新results目錄。freeze在第二輪績效前記錄程式、設定、既有結果及來源hash，完成時再核對。若缺陷修正另寫紀錄、新output，不覆寫舊績效。

只取第一輪13個SIMPLIFIED_CONDITIONAL日期及其canonical封存run，不擴增股票/日期，不用latest。訓練、驗證與原回溯留出標籤保留，但13日都已被看過，**全部是已知探索資料，沒有新未觸碰樣本外**。新日期前瞻驗證PENDING，不啟動排程。

起始190000元，每日一筆、一倉，3500元風險預算、整1000股；本金及費用上限、跨日權益及剩倉阻擋沿用第一輪。3500不是最大損失保證。股/張、逐筆量語義、歷史資格及限制、五檔exchange clock、實際帳戶費率仍未證，STRICT_EXECUTABLE的收益/回撤/交易數維持NA，不能把安全拒絕的零交易當績效。

已知歷史先賣後買暫停、處置/停牌必須排除；資格表與策略條件分開記錄。只使用原封存當日證據，未知標UNKNOWN，不從今日表回填。狀態/資格缺漏不能宣稱合法進場。沿用原代理手續費/稅率，不稱實際對帳費率。

## 第一輪核對及S1候選

A沿用原LONG訊號selector，但第一輪另加300秒連續暖機/開盤觀察、auction flow處理及延遲容量模型，並非原paper的即時全額成交帳。詳precheck；不以舊SPEC或對話取代現碼。

C與D要逐scenario列交易signal_id、entry fills/數量/時間及差異，不只比損益。0050不可評估時，LONG狀態是UNKNOWN，不是沒有合格LONG。本輪不新增D策略績效；另輸出每30秒0050 readiness以標明這個限制。

R0使用第一輪S1_NONE原候選signal_id/time/score/排序/反彈狀態，不重建不同S1。S1仍須先低於VWAP、有真正反彈回測、凍結已知回測區間後再轉弱；flow<=-0.35、大單<=-0.25，開盤/300秒暖機/新鮮度/spread與第一輪相同。0/1內外盤只是vendor proxy，並非已驗證真正aggressor。五檔不平衡保留原排序成分與診斷，不新增硬門檻。

新增條件統一在原S1候選產生後篩選；不使被擋候選重新武裝、不改setup後續生命週期。這使R1對應第一輪S1_WEAK，所有版本共享同一候選全集，不把換狀態機混入條件研究。每次仍需重新做帳戶仲裁，不能從舊總績效扣掉被擋虧損。

## 六個進場版本，全部E0

|版本|僅新增條件|
|---|---|
|R0|不加條件，原S1_NONE完整設定|
|R1|0050當時價格低於已收到資料形成的當日VWAP，且300秒報酬<=0|
|R2|RS300=個股300秒報酬−0050同期間報酬<=−0.002|
|R3|VWAP(t)<VWAP(t−180秒)|
|R4|原entry_distance_fraction<=0.003，而非原0.005|
|R5|僅R1與R2同時成立|

不做其他排列、門檻或參數搜尋。所有新值是固定假設，不稱最佳。R1原features與重建值需一致，R2優先同一歷史features並核對；不足0050/300秒資料時R1/R2/R5 evaluable=false、pass=null，與條件已知不成立分開。

R3兩個VWAP都在其receipt-time cursor保存，不用後來完整日量逆推。t−180時可尚未暖滿300秒，但必須有開盤累積VWAP、當時fresh tick、非暫停且來源不缺；該狀態另列。指標不以未收到的exchange time事件更新。

## 兩種公平比較及資料範圍

OWN_EVALUABLE：每版本只於自身可評估S1候選時點測條件；缺資料拒絕另列，不當false。COMMON_EVALUABLE：以每個原S1 signal_id在R0..R5均可評估的交集為固定候選範圍（不是全部條件都通過的交集），R0亦在該交集重新重播。無關股票的缺資料不刪掉另一股有效候選。

輸出每候選mask、每30秒0050 readiness、各日有效/不可評估候選數、日期清單與可觀察決策時段。報告同時列13個封存日、實際有可評估候選的日數、進場日數；不能把13日稱全部股票/時段完整。沒有部位的模型現金0損益僅指已評估範圍，不推測UNKNOWN時段的策略表現。

相同時間保留第一輪score降序、股票碼排序、每決策只一次IOC attempt；未來未成交結果不能回頭改選同時另一檔。當日已進一筆即其他訊號記DAILY_LIMIT，SHORT proceeds不增額度。剩倉、無法估值不能刪除或零填。

## 出場：固定進場配對與完整帳戶分開

固定配對：每一成交情境使用其**自身第一輪B/S1_NONE的原始entry cohort、requested/filled quantity及entry fills**，E0/E1/E2同批比較；BASE八筆，其他情境依原有效紀錄。重播原entry並精確驗證fills，不能用BASE fills配3秒進場卻稱相同。僅本地函式依賴注入原requested quantity，不修改第一輪全域或原碼。E0必須與第一輪逐筆入場/退出/費稅/損益一致，不符即停止，不按結果改規則。

E0完全沿用第一輪3500淨停損/MFE_V1/反向確認與13:20開始回補、13:25截止。E1加進場前已形成反彈高點+1個合法tick被突破；E2持有>=300秒、net_R<0.25、買方flow>0.10且大單方向>0。順序與第一輪相同，觸發不是成交，延遲、新五檔、部分成交/未完成保留。

price_R=原純engine反推的每股初始停損風險；net_R=模型淨損益/3500。兩者不改名混用。

ACCOUNT_EXIT：另外使用相同S1_NONE候選，E0/E1/E2各從190000完整跨日重播，資金路徑可產生不同數量/候選；不能當固定配對因果效果。第一輪既有10日E1/E2保留reference，核對可重用，但不冒充新13日或同批進場。**不把較好進場與全部出場交叉搜尋**。

## 成交及單筆原因診斷

BASE、WORSE、SHARES假設逐項沿用第一輪：1/3秒延遲、不利1/2tick、五檔10%整張容量、進場IOC限價、同price/depth不重複補容量，費率同原碼代理。資料修正版本次沒有；若日後確認單位/費率，需另開全組共同修正版，不混算。

八筆BASE原空單逐筆列signal/entry/exit時間、價量、費稅、原特徵、0050/RS/VWAP、容量與缺漏。1/3/5分鐘以entry fill後各target cursor前最後新鮮ask作固定原量的hypothetical quote mark，不能取target後的報價。若已出場，同時列actual realized，不能將hypothetical當仍有持倉；缺新鮮報價為NA。

MFE/MAE時間從已觀察path保留；原MFE下限0若沒真實0點不假造時間，另列實際觀察peak/trough。positive_peak=max(0,observed_peak)，浮盈已回吐量=min(positive_peak,max(0,positive_peak−final_net))；peak-to-final drawdown另列（包含新增虧損），避免把全部虧損都稱曾有浮盈。原giveback欄不覆寫。

原因可重疊：60秒可觀察gross<0、及60與180秒皆gross<0分別保留直接逆向描述；distance>.003是偏晚位置代理；positive_peak>0且final_net<peak為浮盈回吐；gross>0而net<=0為成本翻負。缺資料UNKNOWN，不硬推因果。即使gross<0仍列成本侵蝕的金額，但不說成本造成全部虧損。滑價僅分解已知模型不利tick及收到報價間價格變動，不能把無反事實重播的總差額都叫可消除滑價。

## 統計、選擇及交付

每組：候選/可評估/缺資料/條件拒絕/資金成交拒絕/入場/完成數、獨立日期、成本前後、平均/中位、勝率/PF、MFE/MAE/持有/出場/回吐、最大單筆損失、最差日、含未平倉的盤中及日終回撤、相對同scope R0與空手增量、扣最佳日/筆集中度。沒有完成交易勝率/PF為NA；未平完整績效NA且曝險列示。

被擋舊獲利/虧損signal配對僅診斷，另列若帳戶改做其他候選的完整結果。每組自己scope與共同scope不能相加，情境/配對/帳戶重疊亦不能當獨立樣本。

保留原至少10筆完整、5個交易日、正淨利門檻，不降低；即使達數量門檻，這批已知資料也不宣稱新樣本外/正期望，不自動選用/啟用。少量轉正只是候選現象，不給逐筆獨立bootstrap或年化。

交付報告、SPEC/config、precheck/C-D signal_id清單、八單診斷、全部實驗/交易/訊號/每日/有效mask/擋訊號/執行異常、測試、manifest及重現入口。私下ZIP白名單排除帳號/憑證/token、完整raw feed、帳戶DB與Git，保留SHA及去識別對照；GitHub僅研究程式，不公開結果。
