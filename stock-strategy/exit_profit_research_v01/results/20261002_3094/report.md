# 3094 聯傑出場 overlay 紙上診斷

## 研究邊界

- 僅紙上 replay；沒有修改正式策略、LIVE gate、execution routing 或正式 runtime。
- Variant A 是既有 `HARD_3500_PLUS_MFE_V1_NO_LOSS_RECOVERY`。
- 只在 A 之上競爭兩類額外 exit overlay：causal prior-high rejection，以及 same-price + persistent five-level ask pressure。
- 既有 B 保持 frozen，未修改也未納入調參。
- 固定 6 組 preregistered 組合，未依結果優化。
- 事故實際人工處理路徑與正常 A replay 分開，不混為策略績效。

## 資料資格

來源 run：`20261002T005007.842444Z_e3c519f7`

- manifest status：`COMPLETE`
- mode：`SHADOW_ONLY_READ_ONLY_QUOTES`
- 3094 ticks：11,261
- 3094 five-level books：21,695
- 全 run callback errors：0
- 時段：08:50:07–13:35:09 台北時間，涵蓋進場前狀態與 13:20 強平時點
- 五檔有五層價格、五層數量與 callback received time
- 限制：five-level 沒有獨立 exchange timestamp，舊版 archive 也沒有 per-event ingest acceptance/sequence evidence。因此可做因果 callback-time 診斷，但不具 production semantic-parity 證明。

五檔掛單消失只視為取消/移動的可能性，從未當成成交。紙上成交只使用觸發後觀察到的 bid price/quantity，2,000 股以 2 張逐層吃 bid；五層不足即 `UNSCORABLE`。

## 正常 Variant A replay

固定使用事故實際進場：2,000 股、均價 70.05、確認成交時間 09:13:31。

| 指標 | 結果 |
|---|---:|
| 已知進場前高點 | 71.00 |
| MFE 可執行 bid | 72.60 |
| MFE 淨損益 | +4,637 元 |
| A exit trigger | 09:19:54.107，`MFE_PROFIT_PROTECTION` |
| 250ms 模型首個足量五檔 | 09:19:54.855，bid 71.60 有 2 張 |
| 模擬成交價 | 71.60 |
| 費稅後淨損益 | **+2,642 元** |
| 從 MFE giveback | 1,995 元 |

這是「若 runtime 沒有在 09:13:45 crash」的正常策略路徑，不是事故實際損益。

## Overlay 比較

| Variant | 額外 overlay 有先觸發？ | 淨損益 | 對 A 差異 | winner cut |
|---|---:|---:|---:|---:|
| A_BASELINE | - | +2,642 | 0 | 否 |
| PRIOR_HIGH_1 | 否 | +2,642 | 0 | 否 |
| PRIOR_HIGH_2 | 否 | +2,642 | 0 | 否 |
| PRIOR_HIGH_3 | 否 | +2,642 | 0 | 否 |
| SAME_PRICE_ASK_1 | 否 | +2,642 | 0 | 否 |
| SAME_PRICE_ASK_2 | 否 | +2,642 | 0 | 否 |
| SAME_PRICE_ASK_3 | 否 | +2,642 | 0 | 否 |

所有 overlay 都要求：淨浮盈為正、靠近已知 causal prior high、breakout/touch 後失敗、buyer flow weakening；same-price 組另外要求相同 best ask 維持多次且跨越指定秒數，並有五檔 ask/bid depth ratio。沒有任何組合在 A 的 MFE exit 前同時滿足條件。

## 延遲敏感度

0ms、250ms、1000ms 三個固定 latency replay 都在觸發後觀察到 71.60 足量 bid，費稅後淨損益均為 +2,642 元。這只表示該約 1 秒區段的可見深度穩定，不代表真實排隊一定成交。

## 結論

這一天沒有證據顯示阻力/賣壓 overlay 比既有 A 更早、更好地保住獲利；它們也沒有切掉 winner，因為根本沒有先觸發。唯一可支持的結論是：在這筆 3094 路徑上，既有 MFE_V1 已先勝出。

樣本只有 1 筆，不能用來上 production、調參或宣稱 overlay 無效。保留紙上研究即可；下一步需要更多完整交易日、更多已成交/可評分訊號，以及帶 exchange timestamp 與 ingest acceptance sequence 的五檔資料。
