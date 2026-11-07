# GPT-6.1 Terra dashboard coverage — 2026-10-27

Owner requested adding 6.1 Terra to the Grafana dashboard. Deployed dashboard files to the existing tailnet-only hub; no service restart. Changes remain uncommitted and unpushed.

- Added Terra to the generated Codex by model & speed and Codex speed mix panels, with Standard, Fast and no-tag labels. No Terra Ultrafast override.
- Speed collector now includes Terra and retains its diagnostic speed metadata. Credit rates are 25 / 1.25 / 125 per million input / cached input / output tokens.
- Added official Terra API rates: $1 input, $0.05 cached input, $5 output, $1.25 short cache writes and $2.50 long cache writes per million. Fast multiplier 2. Requests above 272,000 input tokens have 2x input/cache and 1.5x output rates. A new optional output multiplier preserves other models' existing pricing behavior.
- Live local metadata showed a 475,000 context window and a maximum Terra prompt of 274,547 tokens. Therefore the usage lane deliberately retains tokscale's estimate for Terra, as it does for other models whose request-level context tiers cannot be resolved from daily aggregates. The Models pricing column remains estimate. The speed lane resolves the tier per request and splits that estimated total proportionally.
- Ran the collector once with --backfill to populate new Terra speed series' historical endpoints. Existing frozen usage ledger was not rebuilt. 7,320 samples pushed; collector receipt HEALTHY. Future scheduled runs use this working tree automatically.

Validation: all 356 tests passed, including Terra standard/fast pricing below, at and above the context threshold. Generated dashboard lint and git diff --check pass. Preview screenshots at 1920 and 1440 widths showed Terra; preview file then removed from the hub. Live Grafana API confirms Terra in both panels. Evidence and logs are gitignored under private/terra-20261027/.

Sources fetched 2026-10-27:
- https://developers.openai.com/api/docs/models/gpt-6.1-terra
- https://learn.chatgpt.com/docs/pricing
- https://learn.chatgpt.com/docs/agent-configuration/speed

Rollback: restore only this change's five source/test/dashboard files from the pre-change HEAD, regenerate estate.json, and rsync dashboard files per AGENTS.md. No service restart is needed. New metric series can remain stored. Do not reset the usage ledger or collector state.

## Follow-up 2026-11-08

The owner authorized committing and pushing the combined dashboard changes, including this existing Terra support. See `2026-11-08-client-speed-coverage.md`; the earlier uncommitted status above describes the original 2026-10-27 handoff.
