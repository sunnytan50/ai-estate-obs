# Fable 5.2 dashboard coverage — 2026-11-10

Owner requested the existing Grafana dashboard be updated for Fable 5.2 on 2026-11-10.

Added claude-fable-5-2 pricing effective 2026-11-09 (release date): input $8, output $40, cache read $0.20, 5-minute write $10, 1-hour write $16 per million tokens. Fast multiplies all categories by 2. Existing model rates were preserved. Added Fable 5.2 display overrides for the Claude Code and Droid model/speed panels; regenerated estate.json. No client model or billing configuration changed.

Validation: 367 tests pass, including release-date gating, normalized dated Fast ID, all token-category valuation and preservation of Fable 5.1 pricing. Generated dashboard and git diff checks pass. Initial test run caught the not-yet-regenerated dashboard; regenerated and reran the entire suite successfully.

Live: collector pushed 252 samples and reported HEALTHY; observed Fable 5.2 Claude Code usage in VictoriaMetrics. Existing 246 frozen usage days and 114 frozen client-speed days were compared as decoded records and are unchanged. No backfill. The existing launchd job reads this working tree; no schedule or service restart needed.

Preview and production browser checks at 1920 and 1440px show Fable 5.2, retaining Astra Fast/Ultrafast, with no page errors. Production panel definitions match the local generated dashboard through the Grafana API. Both Claude Code and Droid speed breakdowns reconcile to usage model totals. Dashboard-only publication to the existing tailnet hub; temporary preview removed.

Evidence and rollback: private/fable52-20261110/ holds tests, collector log, state/receipt before changes, live dashboard before/after, query results and screenshots. Restore the saved live-before.json dashboard object to the provisioned estate.json path for a display rollback. Do not restore collector state or rewrite frozen days. Owner authorized committing and pushing the source changes on 2026-11-10. The Git receipt is recorded in the shared coordination task aiobs-fable52-20261110.

Sources verified live using Firecrawl on 2026-11-10 Dubai:
- https://platform.claude.com/docs/en/about-claude/pricing
- https://platform.claude.com/docs/en/models/fable-5-2/overview
