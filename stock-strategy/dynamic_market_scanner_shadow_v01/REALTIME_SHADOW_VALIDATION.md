# Realtime expanded shadow validation

The frozen observer was replayed against the sealed 2026-11-09 full-session
archive before scheduler integration.

- Source run: `20261109T005013.614083Z_69332fe7`
- Source manifest hash:
  `d40589319157add82b54881374327cd663c74151603fecd516f68720722198f5`
- Events offered and processed: 2,362,903 / 2,362,903
- Dropped events and observer errors: 0 / 0
- Selected signal: 6456 GIS-KY at 09:14:30, entry TWD 78.8
- Stored post-session V2 selection: identical symbol, decision time, and entry
  price
- Relative strength at selection: 0.0157749
- Three-minute raw best-bid mark: TWD 79.1, after-cost P&L +TWD 91
- Three-minute adverse-tick executable proxy: TWD 79.0, after-cost P&L
  -TWD 108
- Actual orders, fills, and broker order calls: 0 / 0 / 0

The signal-selection parity passed, but the proposed three-minute continuation
gate did not pass under the conservative executable proxy. Therefore the older
positive result based on the raw best bid is not treated as live-ready
evidence. New scheduled full sessions must accumulate prospectively; no policy
parameter is changed from their results.
