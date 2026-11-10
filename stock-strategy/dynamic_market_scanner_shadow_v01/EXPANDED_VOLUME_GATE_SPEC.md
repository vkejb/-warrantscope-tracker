# EXPANDED_FROZEN_VOLUME_ACCELERATION_V2

This study applies the already frozen `volume_flow_entry_study_v01` V2 rule to
the expanded-universe current-engine candidate ledger without changing a
threshold:

- recent and previous windows are 30 seconds;
- each needs at least five ticks;
- recent directional volume delta must be at least 0.35;
- recent volume / previous volume must be at least 1.0.

V0 uses no additional volume gate. V2 uses only the rule above. At each causal
decision time, choose the highest production score among candidates that pass;
the first successful time consumes the daily TWD 190,000 shared-capital slot.

BASE uses the original proxy entry. STRESS waits five seconds and adds one
additional adverse tick beyond the existing proxy. Both use fee-aware whole
lots and the unchanged production exit engine. No additional volume threshold,
combination, or execution scenario is searched.

The parameters predate this expanded-universe outcome study, reducing but not
eliminating selection bias. The 16 archives are still known data; new-date
validation is required for promotion.
