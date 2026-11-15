# Fair A B replay correction

Frozen 2026-11-15 before the corrected fair comparison was calculated.
The earlier `SPEC.md` and first two outputs are retained as archival-behavior
diagnostics. They inadvertently used the 11/12 deployed candidate reservation
as arm A, whereas a fair A/B must use the same candidate admission for both.
This is a methodology correction, not a parameter search.

- First report the archived 11/12 behavior separately: 6770 reserved the only
  continuation slot despite failing the +1.00 percentage-point challenger;
  1528 was never given a three-minute continuation check.
- Fair A and B both require the frozen V2 volume gate and the frozen +1.00
  relative-strength gate **before** candidate selection. Both choose the
  earliest eligible time, highest production score, then symbol. Same stock,
  one trade/day, 190,000 TWD, and same exit engine.
- Fair A waits 180 seconds from the original signal and requires a fresh
  received tick at the due time with positive after-cost net using the original
  signal entry price, quantity, and the existing conservative long exit quote.
  It then enters using the same delay/ask-plus-tick proxy as B.
- B enters using that proxy immediately after the signal. Both arms report
  BASE 1 second/one tick and STRESS 5 seconds/two ticks. Both remain conditional
  quote proxies: depth units and queue position are unverified.
- A missing/stale continuation is `UNSCORABLE`, not a failed continuation or
  zero return. 11/13 remains partial to 10:18 and is not a full day.
- Earlier 18-day pre-3-minute V2 results remain separate exploratory context.
