# Liquidity-qualified early-stop validation

Post-hoc hypothesis: the unchanged 120-second failure stop may exit only when the latest 30 seconds contain enough fresh trades and volume relative to the preceding 30 seconds. Thin activity means insufficient evidence. Hard stop and MFE protection remain active.

## Base signals

| Rank | Min ticks | Min volume ratio | Net PnL | vs immediate | PF | Avg loser | Max DD | Qualified exits | Thin holds | Holds profitable | Holds losses | Winners harmed | LOTO robust |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 1 | 0.10 | 2445 | +4489 | 1.168807 | -2414 | 12995 | 4 | 1 | 1 | 0 | 0 | NO |
| 2 | 3 | 0.10 | 2445 | +4489 | 1.168807 | -2414 | 12995 | 4 | 1 | 1 | 0 | 0 | NO |
| 3 | 5 | 0.10 | 2445 | +4489 | 1.168807 | -2414 | 12995 | 4 | 1 | 1 | 0 | 0 | NO |
| 4 | 1 | 0.25 | 1647 | +3691 | 1.107774 | -2547 | 13793 | 3 | 2 | 1 | 1 | 0 | NO |
| 5 | 1 | 0.50 | 1647 | +3691 | 1.107774 | -2547 | 13793 | 3 | 2 | 1 | 1 | 0 | NO |
| 6 | 3 | 0.25 | 1647 | +3691 | 1.107774 | -2547 | 13793 | 3 | 2 | 1 | 1 | 0 | NO |
| 7 | 3 | 0.50 | 1647 | +3691 | 1.107774 | -2547 | 13793 | 3 | 2 | 1 | 1 | 0 | NO |
| 8 | 5 | 0.25 | 1647 | +3691 | 1.107774 | -2547 | 13793 | 3 | 2 | 1 | 1 | 0 | NO |
| 9 | 5 | 0.50 | 1647 | +3691 | 1.107774 | -2547 | 13793 | 3 | 2 | 1 | 1 | 0 | NO |

## Expanded signals

| Rank | Min ticks | Min volume ratio | Net PnL | vs immediate | PF | Avg loser | Max DD | Qualified exits | Thin holds | Holds profitable | Holds losses | Winners harmed | LOTO robust |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 1 | 0.10 | -9382 | +4489 | 0.79488 | -2407 | 30475 | 13 | 1 | 1 | 0 | 0 | NO |
| 2 | 3 | 0.10 | -10879 | +2992 | 0.769688 | -2486 | 31972 | 12 | 2 | 1 | 1 | 0 | NO |
| 3 | 1 | 0.25 | -12175 | +1696 | 0.749135 | -2554 | 33268 | 11 | 3 | 1 | 2 | 0 | NO |
| 4 | 5 | 0.10 | -12874 | +997 | 0.738498 | -2591 | 33967 | 11 | 3 | 1 | 2 | 0 | NO |
| 5 | 1 | 0.50 | -13172 | +699 | 0.734055 | -2607 | 34265 | 10 | 4 | 1 | 3 | 0 | NO |
| 6 | 3 | 0.25 | -13672 | +199 | 0.726719 | -2633 | 34765 | 10 | 4 | 1 | 3 | 0 | NO |
| 7 | 5 | 0.25 | -13672 | +199 | 0.726719 | -2633 | 34765 | 10 | 4 | 1 | 3 | 0 | NO |
| 8 | 3 | 0.50 | -14669 | -798 | 0.712519 | -2686 | 35762 | 9 | 5 | 1 | 4 | 0 | NO |
| 9 | 5 | 0.50 | -14669 | -798 | 0.712519 | -2686 | 35762 | 9 | 5 | 1 | 4 | 0 | NO |

## Finding

- Immediate reference: -2044 TWD, PF 0.876121, max drawdown 14100 TWD.
- Best in-sample base variant: LIQUIDITY_QUALIFIED_TICKS_1__VOLUME_RATIO_0.10 at 2445 TWD (+4489).
- The same variant on expanded signals: -9382 TWD (+4489).
- Classification: NO_ROBUST_LIQUIDITY_QUALIFICATION_EDGE.
- Because the idea came from inspecting 2221, even a strong result is not production evidence; only untouched future shadow sessions can validate it.
- No production or live behavior changed. The least-aggressive cell is added only as a future post-session paper-shadow challenger; it cannot place orders.

## Limitations

- This hypothesis was motivated by inspecting the same sample and is explicitly post hoc.
- Only ten base signals and eighteen overlapping near-misses are available.
- Trade-count and volume-ratio thresholds need untouched future sessions before any promotion.
- Older independent signals and near-misses are not one executable portfolio.
- The 20260930 session is stitched and has four untimestamped callback errors.
