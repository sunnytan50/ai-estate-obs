# Astra speed telemetry closeout — 2026-10-02

The speed lane and Grafana panels distinguish observed tokens, an allowance comparison index, and a purchased-credit scenario. Unknown mode/authentication remains unclassified; confirmed API-key traffic is excluded from subscription estimates. These panels do not measure actual account quota or money charged.

## Source verification

[OpenAI pricing](https://learn.chatgpt.com/docs/pricing) was fetched again on October 2 GST. Astra Standard weights are 250/25/1250 credits per million uncached input/cached input/output. Standard/Fast/Ultrafast multipliers are 1/2.5/8 for included allowance and 1/2/6 for purchased credits.

## Review and corrections

A read-only Claude subscription peer review identified a late-diagnostics race: moving an emitted unknown event into a known-mode series could count its tokens twice under monotonic counter preservation. Recent unknown events now wait two minutes for diagnostics. Successfully emitted classifications are cached locally and remain stable; late metadata does not upgrade old unknown history. The prior metadata snapshot is used when migrating existing collector state. Cache retention is bounded to 400 days and keeps model-change boundaries for Astra turns.

Malformed metadata is rejected without failing the lane. Collector state is written through a private temporary file, synced, and atomically replaced; failed serialization preserves the previous offsets and attribution cache.

Regression checks cover late metadata, grace timing, legacy cache migration, corrupt metadata, private state permissions, and failed-write preservation. The full workstation suite passed 279 tests on the final source at this checkpoint. The real metadata dry-run returned all three enabled lanes up, with no stderr and populated Standard/Fast/Ultrafast/Unknown series. The normal scheduled collector persisted the new attribution cache successfully without a service restart.

## Responsive verification

The live provisioned dashboard matches the local JSON byte hash. All 33 non-row panels fit at 390, 768, 1024, 1280, 1440, 1728, and 1920 CSS pixels with the navigation menu both open and closed. Document width equals viewport width in all 14 cases. No dashboard layout modification was needed: the earlier screenshot-wide right-edge clipping was not reproducible in the current page. Screenshots and numeric probes are kept in ignored local output rather than Git.
