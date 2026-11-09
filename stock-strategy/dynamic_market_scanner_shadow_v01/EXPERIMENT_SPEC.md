# Frozen specification

- Premarket source: existing sealed expanded universe, maximum 400 symbols.
- Premarket price cap: prior-session close must be at most TWD 190.
- Intraday source: causal diagnostics from the production LONG signal engine.
- Observation state: a base signal awaiting required consecutive confirmation.
- Ready state: all existing production gates pass but another candidate ranks higher.
- Paper selected: existing production engine selects the candidate.
- Rejection: price above TWD 190, unaffordable whole lot after entry commission,
  existing production gate failure, or active-observation capacity.
- Expiry: an observing/ready candidate is absent at the next diagnostic window.
- Active observation capacity: 40 candidates, ranked by existing production score.
- Capital: TWD 190,000; whole board lots only; entry commission is included in
  the affordability check.
- Outputs remain shadow-only. `actual_orders=0`, `actual_fills=0`, and
  `broker_order_calls=0` are mandatory.
