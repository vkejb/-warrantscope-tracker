# EARLY_ROTATION_ACCELERATION_STUDY_V0_1

本研究是最後一輪 EOD Dynamic Peer 擴充，只研究 Frozen Stage A Top30 中「Level 尚未過高、但 RS／turnover activity／breadth／volume 正在加速」是否比 mature rotation 有較好 path。它不是新選股器，不接券商、不下單。

## Frozen contract

- Stage A、每日 Top30、outcomes、costs 完全沿用 immutable artifacts，refit count 為 0。
- Dynamic Peer 完全沿用上一輪 Top10 correlated peers、60-session window、至少40共同sessions、leave-one-out，不重新選 peers。
- Official Sector 已是 `OFFICIAL_SECTOR_PIT_UNSAFE`，本輪不重測。
- 2020–2022 discovery；2023–2024 retrospective confirmation not blind OOS；2025 stress/prevalence-seen not blind。

## Preregistered definitions

- RS acceleration：同日 Stage A Top30 中 `peer RS3` percentile 減 `peer RS20` percentile。
- Turnover acceleration：`log(share_T/share_T-3)/3 - log(share_T-3/share_T-20)/17`；只稱 capital activity proxy。
- Breadth acceleration：同一 frozen peer basket 的 breadth(T) 減 breadth(T-3)。
- Volume acceleration：peer median volume-ratio5(T) 減 T-3。
- LEVEL_INDEX：四個 frozen Level features 各自依 discovery empirical CDF 後取平均。
- ACCELERATION_INDEX：六個 preregistered acceleration features依 discovery empirical CDF後取平均。
- LOW/MID/HIGH：只用 discovery index terciles。
- EARLY_ROTATION：Level LOW/MID 且 Acceleration HIGH。
- MATURE_ROTATION：Level HIGH 且 Acceleration MID/HIGH。

Composite 只有 discovery 中至少兩個不同 acceleration families 的高 quintile 同時提高 success、降低 downside-first 才啟用；絕不反轉不利 feature 的方向。Top15/10/5 是預註冊診斷，不能依 later period 挑 K。

Lead-time 使用 T+1 後 peer context 只做描述性 transition 診斷，絕不進入 signal、threshold 或 classification features。
