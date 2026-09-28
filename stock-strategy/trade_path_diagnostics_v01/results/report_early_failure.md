# EARLY_FAILURE_EXIT controlled diagnostic

Backtest/shadow research only. No stock selection, entry, sizing, existing exit, MFE protection, broker, or live behavior was changed.

## Method

- 17 scorable historical trades; 75 fixed grid candidates.
- A rule exits only when current net PnL R is at/below the loss threshold AND MFE R so far is at/below the progress threshold.
- Current PnL R uses the NT$3,500 net-risk budget; MFE_R/MAE_R use side-aware price excursion divided by the existing initial per-share price risk.
- Observation and simulated fill quotes must each satisfy the existing 5-second freshness rule.
- Original exits that occur first retain priority. All fees, tax, adverse quote proxy, and current exits remain unchanged.
- Ranking prioritizes avoiding eventual winners, breadth across losers, loss reduction, expectancy/PF, and leave-one-trade-out stability—not maximum PnL.

## Best zero-false-exit candidate at each checkpoint

| Checkpoint | Candidate | Affected | Losers improved | Winner false exits | Net delta | PF delta | Avg loser delta | LOO min delta | Fragile |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| 5m | T5_NEG0.50R_MFE0.10R | 2 | 2 | 0 | 1,198 | 0.045 | 109 | 199 | YES |
| 10m | T10_NEG0.30R_MFE0.10R | 3 | 1 | 0 | 1,195 | 0.045 | 109 | -601 | YES |
| 15m | T15_NEG0.20R_MFE0.10R | 1 | 1 | 0 | 999 | 0.037 | 91 | 0 | YES |

## Robust candidates

No candidate met the predeclared robustness screen.

## Concentration warning

The leading shadow-only family is T5_NEG0.50R_MFE0.10R: net PnL changes from -1,778 to -580 TWD, PF from 0.932 to 0.977, and max drawdown by -1,198 TWD. It affects 2 losing trades and no eventual winners in this sample.
However, the largest improved trade supplies 83.4% of the gain. This is classified as outlier-concentrated even though leave-one-out net benefit stays above zero.
The ten leading 5-minute combinations are behaviorally identical in this sample: -0.50R or -0.60R current loss with any tested 0.10R–0.50R MFE ceiling affects the same two trades. The data therefore does not identify a preferred MFE-progress threshold.

## Answers

1. Early identification evidence is evaluated on fresh/open samples of 5m=9, 10m=8, 15m=4 trades; conclusions are necessarily provisional.
2. The most promising checkpoint for further observation is 5 minutes (T5_NEG0.50R_MFE0.10R); no checkpoint passed the full robustness screen.
3. Zero-winner-harm, multi-loser candidates improving net/PF: 10; fully robust candidates after concentration and LOO checks: 0.
4. Shadow-mode eligibility: YES for non-executing measurement only; no live or strategy enablement is justified.
5. The three partial sessions and 17 scorable trades remain far too small for production.
6. Next data needed: more complete trading days across rising/falling/range regimes, continuous fresh quotes, five-level book snapshots, signed inside/outside volume, spread, volatility, and actual manual/live fill timestamps.

All parameter rows, per-trade impacts, and every leave-one-trade-out rerun are in the accompanying CSV files.
