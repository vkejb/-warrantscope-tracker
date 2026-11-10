# CAUSAL_RELATIVE_STRENGTH_AUCTION_5M_V1

This single frozen structural hypothesis replaces first-arrival selection with
five-minute causal auctions aligned from 09:05 through 13:10. A signal belongs
to `[window start, window close)`. At close, only the cohort already known is
replayed through the full current production entry engine. This rechecks current
0050 regime, relative strength, VWAP, breakout, flow, book, spread, anti-chase,
buy concentration, TWD 190, and fee-aware board-lot affordability.

Among candidates that still pass, rank current five-minute relative strength,
then current production score. Enter the winner at the model's current
ask-plus-one-tick proxy. The first successful window consumes the one daily
trade and shared TWD 190,000 capital. Existing exits and costs are unchanged.

Only this five-minute window and ranking are tested. No alternative window,
threshold, or cross-product is searched. The hypothesis was designed after the
16 archive dates were inspected, so those results remain exploratory even
though every decision inside the replay is causal. Promotion requires new dates.
