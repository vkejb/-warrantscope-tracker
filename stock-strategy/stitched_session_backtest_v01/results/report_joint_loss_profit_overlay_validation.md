# Joint loss-control and MFE profit-protection validation

The same 28 fixed entries, quantities, market paths and cost model are replayed. The production reference is CURRENT_LOSS + MFE_V1; no live behavior changed.

## Base signals

| Loss overlay | Profit overlay | Net PnL | vs reference | PF | Avg winner | Avg loser | Max DD | Retention | MFE exits (loss) | Winners cut |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_LOSS | NO_MFE | -12321 | -2694 | 0.488118 | 5874 | -3009 | 20386 | 68.0% | 0 (0) | 1 |
| CURRENT_LOSS | MFE_LOOSE | -7732 | +1895 | 0.633172 | 6673 | -2635 | 17394 | 61.1% | 3 (1) | 1 |
| CURRENT_LOSS | MFE_V1 | -9627 | +0 | 0.543268 | 5726 | -2635 | 17394 | 58.5% | 3 (1) | 0 |
| CURRENT_LOSS | MFE_AGGRESSIVE | -10025 | -398 | 0.524386 | 5526 | -2635 | 17394 | 52.4% | 3 (1) | 1 |
| CURRENT_LOSS | NET_MFE_LOOSE | -4539 | +5088 | 0.779199 | 5339 | -2937 | 16873 | 64.4% | 2 (0) | 0 |
| CURRENT_LOSS | NET_MFE_V1 | -6135 | +3492 | 0.701562 | 4807 | -2937 | 16873 | 55.2% | 3 (0) | 1 |
| CURRENT_LOSS | NET_MFE_AGGRESSIVE | -4439 | +5188 | 0.784064 | 5373 | -2937 | 16873 | 62.9% | 3 (0) | 0 |
| PLAIN_120S | NO_MFE | -4139 | +5488 | 0.739489 | 5874 | -1986 | 14399 | 68.0% | 0 (0) | 1 |
| PLAIN_120S | MFE_LOOSE | -2542 | +7085 | 0.840005 | 6673 | -1986 | 14399 | 61.1% | 2 (0) | 1 |
| PLAIN_120S | MFE_V1 | -4437 | +5190 | 0.720733 | 5726 | -1986 | 14399 | 58.5% | 2 (0) | 0 |
| PLAIN_120S | MFE_AGGRESSIVE | -4835 | +4792 | 0.695682 | 5526 | -1986 | 14399 | 52.4% | 2 (0) | 1 |
| PLAIN_120S | NET_MFE_LOOSE | -2343 | +7284 | 0.85253 | 6772 | -1986 | 14399 | 74.0% | 1 (0) | 0 |
| PLAIN_120S | NET_MFE_V1 | -4437 | +5190 | 0.720733 | 5726 | -1986 | 14399 | 55.7% | 2 (0) | 1 |
| PLAIN_120S | NET_MFE_AGGRESSIVE | -3240 | +6387 | 0.796073 | 6324 | -1986 | 14399 | 62.5% | 2 (0) | 0 |
| RECOVERY_AWARE_120S | NO_MFE | -4838 | +4789 | 0.708326 | 5874 | -2073 | 15098 | 68.0% | 0 (0) | 1 |
| RECOVERY_AWARE_120S | MFE_LOOSE | -3241 | +6386 | 0.804606 | 6673 | -2073 | 15098 | 61.1% | 2 (0) | 1 |
| RECOVERY_AWARE_120S | MFE_V1 | -5136 | +4491 | 0.69036 | 5726 | -2073 | 15098 | 58.5% | 2 (0) | 0 |
| RECOVERY_AWARE_120S | MFE_AGGRESSIVE | -5534 | +4093 | 0.666365 | 5526 | -2073 | 15098 | 52.4% | 2 (0) | 1 |
| RECOVERY_AWARE_120S | NET_MFE_LOOSE | -3042 | +6585 | 0.816603 | 6772 | -2073 | 15098 | 74.0% | 1 (0) | 0 |
| RECOVERY_AWARE_120S | NET_MFE_V1 | -5136 | +4491 | 0.69036 | 5726 | -2073 | 15098 | 55.7% | 2 (0) | 1 |
| RECOVERY_AWARE_120S | NET_MFE_AGGRESSIVE | -3939 | +5688 | 0.762525 | 6324 | -2073 | 15098 | 62.5% | 2 (0) | 0 |

## Additional near-misses

| Loss overlay | Profit overlay | Net PnL | vs reference | PF | Avg winner | Avg loser | Max DD | Retention | MFE exits (loss) | Winners cut |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_LOSS | NO_MFE | -32578 | +8180 | 0.405684 | 7413 | -3654 | 33043 | 93.4% | 0 (0) | 0 |
| CURRENT_LOSS | MFE_LOOSE | -36768 | +3990 | 0.27277 | 13791 | -2974 | 36768 | 100.0% | 4 (4) | 0 |
| CURRENT_LOSS | MFE_V1 | -40758 | +0 | 0.193853 | 9801 | -2974 | 40758 | 71.1% | 5 (4) | 0 |
| CURRENT_LOSS | MFE_AGGRESSIVE | -42555 | -1797 | 0.15831 | 8004 | -2974 | 42555 | 58.0% | 5 (4) | 1 |
| CURRENT_LOSS | NET_MFE_LOOSE | -38364 | +2394 | 0.30166 | 8286 | -3434 | 38364 | 100.0% | 1 (1) | 0 |
| CURRENT_LOSS | NET_MFE_V1 | -42155 | -1397 | 0.232653 | 6390 | -3434 | 42155 | 86.3% | 2 (1) | 0 |
| CURRENT_LOSS | NET_MFE_AGGRESSIVE | -41556 | -798 | 0.243556 | 6690 | -3434 | 41556 | 88.4% | 2 (1) | 0 |
| PLAIN_120S | NO_MFE | -20955 | +19803 | 0.481466 | 9728 | -2526 | 21969 | 90.1% | 0 (0) | 0 |
| PLAIN_120S | MFE_LOOSE | -21553 | +19205 | 0.390194 | 13791 | -2079 | 21553 | 100.0% | 3 (3) | 0 |
| PLAIN_120S | MFE_V1 | -25543 | +15215 | 0.277303 | 9801 | -2079 | 25543 | 71.1% | 4 (3) | 0 |
| PLAIN_120S | MFE_AGGRESSIVE | -27340 | +13418 | 0.22646 | 8004 | -2079 | 27340 | 58.0% | 4 (3) | 1 |
| PLAIN_120S | NET_MFE_LOOSE | -26741 | +14017 | 0.34025 | 13791 | -2384 | 26741 | 100.0% | 1 (1) | 0 |
| PLAIN_120S | NET_MFE_V1 | -30532 | +10226 | 0.246719 | 10000 | -2384 | 30532 | 72.5% | 2 (1) | 0 |
| PLAIN_120S | NET_MFE_AGGRESSIVE | -29933 | +10825 | 0.261497 | 10599 | -2384 | 29933 | 76.9% | 2 (1) | 0 |
| RECOVERY_AWARE_120S | NO_MFE | -16166 | +24592 | 0.579054 | 7413 | -2560 | 21969 | 93.4% | 0 (0) | 0 |
| RECOVERY_AWARE_120S | MFE_LOOSE | -20356 | +20402 | 0.403871 | 13791 | -2009 | 20356 | 100.0% | 4 (4) | 0 |
| RECOVERY_AWARE_120S | MFE_V1 | -24346 | +16412 | 0.287024 | 9801 | -2009 | 24346 | 71.1% | 5 (4) | 0 |
| RECOVERY_AWARE_120S | MFE_AGGRESSIVE | -26143 | +14615 | 0.234398 | 8004 | -2009 | 26143 | 58.0% | 5 (4) | 1 |
| RECOVERY_AWARE_120S | NET_MFE_LOOSE | -21952 | +18806 | 0.430173 | 8286 | -2408 | 22501 | 100.0% | 1 (1) | 0 |
| RECOVERY_AWARE_120S | NET_MFE_V1 | -25743 | +15015 | 0.331767 | 6390 | -2408 | 26292 | 86.3% | 2 (1) | 0 |
| RECOVERY_AWARE_120S | NET_MFE_AGGRESSIVE | -25144 | +15614 | 0.347316 | 6690 | -2408 | 25693 | 88.4% | 2 (1) | 0 |

## Expanded unique signals

| Loss overlay | Profit overlay | Net PnL | vs reference | PF | Avg winner | Avg loser | Max DD | Retention | MFE exits (loss) | Winners cut |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_LOSS | NO_MFE | -44899 | +5486 | 0.430837 | 6797 | -3430 | 53429 | 83.2% | 0 (0) | 1 |
| CURRENT_LOSS | MFE_LOOSE | -44500 | +5885 | 0.378813 | 9046 | -2865 | 50785 | 74.1% | 7 (5) | 1 |
| CURRENT_LOSS | MFE_V1 | -50385 | +0 | 0.296662 | 7084 | -2865 | 54523 | 62.7% | 8 (5) | 0 |
| CURRENT_LOSS | MFE_AGGRESSIVE | -52580 | -2195 | 0.266022 | 6352 | -2865 | 56770 | 54.3% | 8 (5) | 2 |
| CURRENT_LOSS | NET_MFE_LOOSE | -42903 | +7482 | 0.431696 | 6518 | -3282 | 54389 | 78.7% | 3 (1) | 0 |
| CURRENT_LOSS | NET_MFE_V1 | -48290 | +2095 | 0.360338 | 5441 | -3282 | 58180 | 67.6% | 5 (1) | 1 |
| CURRENT_LOSS | NET_MFE_AGGRESSIVE | -45995 | +4390 | 0.390738 | 5900 | -3282 | 57581 | 73.1% | 5 (1) | 0 |
| PLAIN_120S | NO_MFE | -25094 | +25291 | 0.554281 | 7802 | -2346 | 36368 | 79.0% | 0 (0) | 1 |
| PLAIN_120S | MFE_LOOSE | -24095 | +26290 | 0.529688 | 9046 | -2049 | 33720 | 74.1% | 5 (3) | 1 |
| PLAIN_120S | MFE_V1 | -29980 | +20405 | 0.414819 | 7084 | -2049 | 37710 | 62.7% | 6 (3) | 0 |
| PLAIN_120S | MFE_AGGRESSIVE | -32175 | +18210 | 0.371975 | 6352 | -2049 | 39507 | 54.3% | 6 (3) | 2 |
| PLAIN_120S | NET_MFE_LOOSE | -29084 | +21301 | 0.484509 | 9112 | -2257 | 38908 | 82.7% | 2 (1) | 0 |
| PLAIN_120S | NET_MFE_V1 | -34969 | +15416 | 0.380202 | 7150 | -2257 | 42699 | 61.3% | 4 (1) | 1 |
| PLAIN_120S | NET_MFE_AGGRESSIVE | -33173 | +17212 | 0.412035 | 7749 | -2257 | 42100 | 67.3% | 4 (1) | 0 |
| RECOVERY_AWARE_120S | NO_MFE | -21004 | +29381 | 0.618047 | 6797 | -2391 | 37067 | 83.2% | 0 (0) | 1 |
| RECOVERY_AWARE_120S | MFE_LOOSE | -23597 | +26788 | 0.534888 | 9046 | -2029 | 34074 | 74.1% | 6 (4) | 1 |
| RECOVERY_AWARE_120S | MFE_V1 | -29482 | +20903 | 0.418891 | 7084 | -2029 | 37212 | 62.7% | 7 (4) | 0 |
| RECOVERY_AWARE_120S | MFE_AGGRESSIVE | -31677 | +18708 | 0.375626 | 6352 | -2029 | 39009 | 54.3% | 7 (4) | 2 |
| RECOVERY_AWARE_120S | NET_MFE_LOOSE | -24994 | +25391 | 0.546479 | 7529 | -2296 | 37599 | 87.0% | 2 (1) | 0 |
| RECOVERY_AWARE_120S | NET_MFE_V1 | -30879 | +19506 | 0.439694 | 6058 | -2296 | 41390 | 71.0% | 4 (1) | 1 |
| RECOVERY_AWARE_120S | NET_MFE_AGGRESSIVE | -29083 | +21302 | 0.472283 | 6507 | -2296 | 40791 | 75.5% | 4 (1) | 0 |

## Finding

- Production-reference replay: -50385 TWD, PF 0.296662, max drawdown 54523 TWD.
- On the ten base signals, PLAIN_120S__NET_MFE_LOOSE reduced the loss from -9627 to -2343 TWD, raised PF from 0.543268 to 0.85253, and cut no reference winner.
- Best result without cutting a reference winner: RECOVERY_AWARE_120S__NET_MFE_LOOSE at -24994 TWD (+25391).
- Its average profitable-trade retention is 87.0%.
- Existing MFE_V1 produced 5 negative MFE exits after activation. This is consistent with price-R breakeven not covering fees/tax and with gaps between observations.
- The cost-aware result is diagnostic only. Its best loss overlay differs between the base and near-miss cohorts, so there is no stable production combination yet.
- A negative net result or PF below 1 is loss reduction only, not a profitable strategy.

## Limitations

- Twenty-eight unique signals are insufficient for production promotion.
- The eighteen near-miss entries overlap and are not feasible portfolio PnL.
- The 20260930 session is stitched and has four untimestamped callback errors.
- MFE retention uses the full observed path, including movement after an earlier simulated exit.
- The recovery-aware thresholds are declared diagnostics, not fitted parameters.

No production or live behavior changed.
