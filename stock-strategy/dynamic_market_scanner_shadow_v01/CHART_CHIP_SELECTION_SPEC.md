# CHART_CHIP_SELECTION_V1 — frozen before outcome calculation

Status: exploratory, historical reconstruction, SHADOW_ONLY, NO_LIVE_ORDER, actual_orders=0.
Frozen date: 2026-11-14. All 18 Oct–Nov 2026 signal dates have already been seen in earlier studies; none is out-of-sample.

Population: every first-per-symbol `EARLY_ENTRY_READY_OBSERVED` in the existing sealed 18-day replay. The unchanged baseline is `CURRENT_RS1_180S`: RS>=1%, 180-second net-positive confirmation, existing quote freshness, spread, whole-lot, 190,000 TWD, one daily trade/position, original exit, fee and tax model. Candidate order: earliest confirmation, then descending RS, then stock code. A rejected first candidate releases the slot to the next eligible candidate; cash is the zero-P&L comparator. All-signal quality is diagnostic, not account P&L.

Fixed presignal features, never signal-day EOD data:

- Chart `CHART`: preceding trading day's close > mean last 5 closes, and mean last 5 closes > mean last 20 closes. Require 20 valid daily bars with positive prices and nonnegative volume. This is a simple established-trend test, not a pattern optimizer.
- Chip `CHIP`: preceding trading day's combined foreign + investment-trust + dealer net share flow > 0, from official TWSE T86 or TPEx institutional daily detail. No margin or same-day chip data are used. Missing, invalid, duplicate or unproven identity is UNKNOWN, not a pass or a negative flow.
- Four variants: BASE, CHART, CHIP, CHART_AND_CHIP. The two-market official historical rows are retrieved after the fact and exposed as `HISTORICAL_RECONSTRUCTION`, not proof that the specific version was captured before each signal. The one-trading-session lag is a conservative availability rule, not proof of original publication timestamp.

Data-calendar correction recorded before final comparison: the local independent calendar erroneously includes 2026-10-23 (typhoon closure) and 2026-10-26 (public-holiday closure). TWSE 2026 holiday information, the 10/23 closure announcement, and both exchanges' empty daily data corroborate that 2026-10-22 is the preceding trading session for 10/27. This corrects data alignment only; no gate, threshold, account or exit rule changes. The original failed 10/26 official lookup remains in the local evidence cache, unused.

Compare on identical 18 dates with no-signal dates kept, BASE and STRESS execution assumptions unchanged. If an account trade cannot be exited/scored, full account P&L and drawdown are NA; scored-only values are separate. Report rejected positive and negative 30-minute signal opportunities, but do not treat these as independent account returns. No threshold search, retuning or production promotion. No broker/API order calls, scheduled jobs, or modifications to formal strategy.
