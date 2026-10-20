# GPT-6.1 Sol speed tiers on the estate dashboard — 2026-10-20

Owner ask: GPT-6 Sol at Standard, Fast and Ultrafast was not shown on the dashboard; include it "in a nice way", in "Where the value went" at the top.

## Done (LIVE, uncommitted — commit/push need the owner's go)
1. `workstation/aiobs_collector/prices.py`: new `gpt-6.1-sol` entry `since="2026-10-19"` adding Ultrafast = 6x (Fast stays 2x). Before this, Sol Ultrafast tokens were valued at Standard. Source: learn.chatgpt.com/docs/agent-configuration/speed, fetched 2026-10-20 ("Ultrafast supports GPT-6 Astra and GPT-6.1 Sol", 6x purchased credits / API). 10-19 was the oldest unfrozen day, so no frozen day changed.
2. `workstation/aiobs_collector/lane_codex_speed.py`: tracks Astra AND Sol (was Astra only). Sol credit rates 50 / 2.5 / 250 per MTok (learn.chatgpt.com/docs/pricing, fetched 2026-10-20). New metric `aiobs_codex_speed_value_usd_total{model,speed,billing}` = list price x speed multiplier. Mode cache now retains Sol turns too, so the usage lane's Sol speed shares survive diagnostic-log rotation.
3. Registered the metric in `__main__._lane_for_sample` and `monotonic.CUMULATIVE_METRICS`.
4. Dashboard (`scripts/build_estate_dashboard.py` -> `estate.json`): new panel 318 "Codex by model & speed" in the top "Where the value went" row (per-day 12 wide, By provider 5, new panel 7). Each model's usage-lane value is split in the proportions of its speed-lane value, so its bars add up exactly to the Models table (checked 7d: Astra $2,126.37, Sol $215.25). Panel 316 "Astra speed mix" became "Codex speed mix" (Astra + Sol tokens). Labels are short on purpose ("Astra Standard", "Sol no tag") — longer ones clip at 1440 wide.
5. Tests: 4 new (355 pass). One-off `--backfill` run pushed the new series' history (the push filter only sends past days newer than each lane's high-water mark, so a new series otherwise has only today's point and a period query counts all its history).

## Evidence
- Screenshots: `output/playwright/sol-speed-preview-1920.png`, `sol-speed-live-1920.png`, `sol-speed-live-1440.png`.
- Speed tags in `~/.codex/logs_2.sqlite` start 2026-10-19 for Sol, so earlier Sol usage shows as "no tag" (valued at Standard).

## Open / noticed, not fixed
- Sol tokens for the last 24 h: Codex session logs show ~536M cache reads, tokscale (usage lane) ~251M. 7-day totals are closer (1,207M vs 1,046M). Likely tokscale lagging active sessions; worth a check (the 10-15 audit found ratio 1.00 on older days).
- Astra: speed-lane value is ~12% above the usage lane over 7 days despite identical token counts (frozen days were valued with the speed shares known at freeze time). The panel avoids the gap by scaling to the usage-lane value.

## Rollback
`git checkout` the 10 files, rerun the rsync in AGENTS.md. The extra metric series in VictoriaMetrics is harmless.
