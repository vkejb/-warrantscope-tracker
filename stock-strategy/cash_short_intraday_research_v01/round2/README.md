# 第二輪：空方條件與出場診斷

此目錄是第一輪的獨立延伸，SHADOW_ONLY、NO_LIVE_ORDER、actual_orders=0；不改正式LONG、Stage A、下單adapter、PROD或排程。原第一輪SPEC、config、results_round1_v2及封存行情保持原樣。

## 執行順序

先執行只讀precheck，核對第一輪原碼、來源及既有結果雜湊。SPEC/config/新增原碼必須在第二輪績效前凍結。新輸出目錄不得覆寫既有研究結果；若有缺陷需留修正紀錄、使用另一個輸出目錄，不能依收益挑規則。

從stock-strategy目錄執行。公開版本不含已凍結的`precheck.py`：該歷史稽核工具內含私有ledger識別碼，僅隨私下ZIP提供，不能把它或其輸出上傳公開remote。初始化需先還原私下ZIP中的同版工具及本機原封存來源；公開checkout本身不具備資料、freeze或完整初始化材料。以下建立流程只適用尚未有`round2/precheck.json`、`round2/freeze.json`的新研究工作副本，**已有檔案不得重建或覆寫**。precheck只接受`--output`；run的`--output`必填。protocol直接執行會凍結，`--verify`僅驗證已有freeze；安全CLI支援`--help`且不觸發凍結（以目前原碼為準）：

```bash
python3 -B -m cash_short_intraday_research_v01.round2.precheck --output cash_short_intraday_research_v01/round2/precheck.json
python3 -B -m unittest discover -s cash_short_intraday_research_v01/round2/tests -v
python3 -B -m cash_short_intraday_research_v01.round2.protocol
python3 -B -m cash_short_intraday_research_v01.round2.run --output cash_short_intraday_research_v01/round2/results_reproduction
python3 -B -m cash_short_intraday_research_v01.round2.report cash_short_intraday_research_v01/round2/results_reproduction
```

已有有效freeze的本機重現，不重建freeze；先只讀核對，再以**全新output目錄**執行run：

```bash
python3 -B -m cash_short_intraday_research_v01.round2.protocol --verify
python3 -B -m cash_short_intraday_research_v01.round2.run --output cash_short_intraday_research_v01/round2/results_reproduction
```

重播需要原本機canonical封存資料及第一輪結果；分享ZIP不附完整行情／帳戶DB／憑證。protocol凍結交易邏輯／測試與既有結果／來源，report與交付展示程式另列authoring；展示修正不得改交易結果。report只讀已完成tables、manifest、precheck，不讀行情、不補跑、不修改規則。run會將已凍結precheck複製至新output。

CSV匯出使用`@oai/artifact-tool`，須先有相容的Node執行環境與套件；Python重播及JSON成果不依賴它。只對已完成的**新output**執行展示／驗證，不重算策略：

```bash
node cash_short_intraday_research_v01/round2/export_tables.mjs cash_short_intraday_research_v01/round2/results_reproduction
python3 -B -m cash_short_intraday_research_v01.round2.deliver cash_short_intraday_research_v01/round2/results_reproduction
```

私下ZIP另需完成的公開源碼發布與獨立審查receipt；`deliver --package`採既定交付白名單並拒絕覆寫既有ZIP，不是任意新資料目錄的通用打包器。交付時若只修正報表／測試載入或路徑遮罩，另留作者層紀錄，不能冒稱事前決策邏輯變動。

## 比較契約

R0為原S1_NONE；R1市場偏弱、R2 RS300≤−0.002、R3 VWAP下彎180秒、R4距回測位置≤0.3%、R5僅R1且R2。所有進場先共用E0。沒有其他門檻排列或事後最佳值。

OWN_EVALUABLE依各版本可評估候選；COMMON_EVALUABLE取每個S1 signal_id在R0～R5可評估性的交集，R0也在交集重播。缺0050時R1／R2／R5是UNAVAILABLE而非false。原13日全部已知、僅探索研究；未看新日期前瞻驗證PENDING，沒有新排程。

FIXED_EXIT按每個成交情境自身原R0 entry cohort與股數比較E0／E1／E2，E0要求第一輪逐筆parity。ACCOUNT_EXIT另做完整資金／每日次數限制重播；兩者不可相加或當相同交易。

## 安全與可信度

19萬元、每日一筆／一倉、3500風險預算、整1000股、原成本及成交情境不變。3500不是最大損失保證；觸發不代表成交，未完成回補保留曝險。

股／張、完整歷史資格／處置／停牌、五檔交易所時間及帳戶費率仍未充分證實；STRICT_EXECUTABLE保持NA。供應商0／1內外盤只作方向proxy。沒有交易的勝率／PF為NA，不把少交易或空手0元稱為獲利策略。

研究程式可公開；freeze、precheck結果、行情、CSV、報告、ZIP及任何真實結果只留私下交付。禁止把研究資料／結果推至公開GitHub。
