# Model speed coverage — 2026-11-08

Owner requested Astra Fast/Ultrafast, Claude Code Opus 5.5 Fast, and Droid fast modes on the existing Grafana dashboard. This work preserves the pre-existing uncommitted Terra changes. The owner subsequently authorized committing and pushing the dashboard changes on 2026-11-08, including the existing Terra support needed to reproduce the live dashboard.

## Implementation

- Existing Astra Fast (2x API value), Astra Ultrafast (6x), and Opus 5.5 Fast (2x) rates were verified against current official documentation. Existing rate entries were not rewritten.
- Added `client-speed` collector lane: Claude Code response usage metadata from both configured Claude projects roots, plus Droid model IDs and tokens from pinned tokscale. A separate daily frozen ledger holds this breakdown. No prompt/response content is exported.
- Added separate Claude Code and Droid model/speed panels in “Where the value went”. Each provider/model joins to its existing authoritative usage total; speed weights are not added to the estate total. Missing retained speed history appears as “no tag”. Unknown-model price splits remain estimates.
- Existing Codex panel stays in place; speed labels are now above the bars to remain readable at 1440px.
- Model normalization now recognizes `-ultrafast` as well as `-fast`, including dated/prefixed model IDs. Known base models with unverified speed pricing retain tokscale estimates instead of silently receiving a Standard price.
- Registered metrics for lane routing, monotonic counters and yesterday's correction window. Enabled the lane in the existing private collector config and documented the example config. The existing schedule is unchanged.

## Validation and live state

- Full suite: **366 tests passed**. Generated dashboard check and `git diff --check` passed.
- Initial collector run pushed **1,226 samples**, including new historical speed endpoints, without `--backfill`; existing frozen history was not re-pushed. Collector receipt reported **HEALTHY**. Peak process-family RSS: **1,709 MiB**, under the 2,048 MiB guard.
- Compared persisted ledgers: all **244** pre-existing frozen usage days were unchanged as decoded records. New speed ledger contains **112** closed days.
- Live VictoriaMetrics query checks: every Claude Code and Droid provider/model speed breakdown sums to its usage-lane Models total (tolerance $0.000001). The checked seven-day window includes positive Opus 5.5 Fast values for both clients.
- Preview at 1920 and 1440px showed Astra Fast, Astra Ultrafast and Opus 5.5 Fast. No browser page errors. Fixed one MetricsQL modifier syntax issue during preview before production publication.
- Production verified through the Grafana API: all three model/speed panels match the local definitions. Live browser checks at 1920 and 1440px show Astra Fast, Astra Ultrafast and Opus 5.5 Fast with no page errors. The temporary preview was removed from the hub.
- Dashboard-only publication to `/opt/observability/grafana/dashboards/estate.json`; no hub deployment/restart, network exposure change, or billing setting change.

## Evidence and rollback

Private, gitignored evidence: `private/speed-20261108/` contains source graph, test/collector logs, query verifier and results, preview/live screenshots, pre-change live dashboard JSON, pre-change collector state, and private config backup. Do not publish this folder or raw account data.

Rollback dashboard: restore the saved `live-before.json` to the same provisioned dashboard path. To disable the new lane, remove only `client-speed` from private `AIOBS_LANES` (or restore the config backup if no subsequent config edits). Do not restore the entire collector state: that would discard subsequent usage. The new metric series can remain in storage. Do not reset existing usage ledgers, and do not revert the earlier Terra edits.

## Sources verified 2026-11-08

- https://learn.chatgpt.com/docs/agent-configuration/speed — Fast 2x purchased/API-equivalent credits, Ultrafast 6x; included allowance multipliers remain separately 2.5x / 8x.
- https://platform.claude.com/docs/en/about-claude/pricing — Opus 5.5 $4/$20 per MTok Standard and $8/$40 Fast; cache modifiers stack.
- https://docs.factory.com/docs/models — current Fast model IDs and distinct Factory credit multipliers; Opus 5.5 1.6x credits vs Fast 3.2x, and Sol Ultrafast ID. These credits are not displayed as dollars.

Commit/push authorization: received from the owner on 2026-11-08. The final Git receipt is recorded in the shared coordination task `aiobs-speed-coverage-20261108`. No owner action is required for the dashboard.
