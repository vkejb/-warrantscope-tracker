# STRICT_MARKET_WIDE_V1

This is a frozen, long-only, post-session causal replay. It is `SHADOW_ONLY`,
has no broker imports or order path, and caps entry price at TWD 190, capital at
TWD 190,000, one board-lot-or-more position, and one trade per session.

The production engine still defines the initial signal. The first approved
signal for each stock enters a 60-second observation. At the checkpoint the
strategy rejects bearish 0050 context, stale quotes/books, excessive spread,
negative signed flow, a bid below the original breakout or causal VWAP, and a
bid that would not cover modeled round-trip costs from the original signal
price. Production anti-chase limits, the TWD 190 cap, and fee-aware whole-lot
affordability are rechecked at the delayed entry.

Only candidates maturing at the same causal checkpoint are ranked. Ranking is
by hypothetical after-cost progress, then relative strength, then the original
production score. The first checkpoint with a passing candidate consumes the
daily slot. Later candidates remain in the ledger and are never backfilled with
end-of-day hindsight.

All features use received data no later than the decision. The historical
expanded archives are already-known exploratory data, not new out-of-sample
evidence. Passing this replay cannot enable live trading.
