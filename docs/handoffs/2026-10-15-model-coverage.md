# Model coverage audit — 2026-10-15

Owner ask: make sure the dashboard is running well and up to date on every model Claude Code, Codex and Droid use. Done; live through the collector (it runs from this working tree). At the owner's go on 2026-10-15 it was committed and pushed with the two earlier uncommitted change sets: 5191b21 (Haiku 5.5 pricing, 10-07), 59e5190 (tokscale runtime repair, 10-14) and 1f4467a (this work).

## Live state (verified 2026-10-15 ~19:00 UTC)
- Pipeline healthy: collector receipt HEALTHY, all 5 lanes succeeded on the first run with the new code (18:54:31Z, 404 samples). Grafana 13.0.2 health ok; hub containers up (VM 5 weeks, Grafana 10 days); GPU exporter and inference scrape `up`, GPU sample age under 2 s. Dashboard renders with Pipeline "All up" (screenshot: `private/model-coverage-20261015/estate-this-month-20261015.jpg`).
- Capture, checked independently per tool over 30 days:
  - Claude Code: the usage lane parses transcripts itself, so every model in use is captured; all are on official Anthropic list prices (pricing page re-fetched 2026-10-15; the uncommitted Haiku 5.5 entry from 10-07 matches it, including 5x per request over 100K prompt tokens). An advisor model is configured (`advisorModel`), but no `advisor_message` usage record exists in 30 days of transcripts, so nothing is dropped today.
  - Codex: dashboard per-day tokens equal a full re-parse of all rollouts including compressed `.jsonl.zst` ones (ratio 1.00 for Sep 8 to Oct 15; script and output in `private/model-coverage-20261015/`).
  - Droid: per-model totals match Droid's own session files. Its bring-your-own-key `custom:OpenRouter-DeepSeek-V4.1-Flash` sessions (60.6M tokens, Sep 19) are counted by the OpenRouter lane as real spend ($0.95), by design, not under droid.

## Changed (commit 1f4467a)
1. `workstation/aiobs_collector/prices.py`: official rates for `gpt-5.6-luna` ($0.20 in, $0.02 cached, $0.25 cache write, $1.20 out; OpenAI model page; no Fast price listed; Codex runs it with a 258,400-token window, largest prompt seen 245K, so the >272K tier never applies) and `kimi-k3` ($3 in, $0.30 cached, $3 5-minute write, $6 1-hour write, $15 out; Kimi API pricing page). Luna's official rate reproduces tokscale's old estimate exactly: its value series continued at $143.6975 and its fallback ("estimate") series stopped after 18:44Z.
2. `workstation/aiobs_collector/lane_usage.py`: `AIOBS_CLAUDE_PROJECTS` is a comma list; every listed root must exist or the run fails (as before). The local, gitignored `config/estate.env` now lists the default projects folder plus a second `CLAUDE_CONFIG_DIR`'s `projects` folder. That folder held about 163M tokens (Opus 5.5, Fable 5.1) on Sep 27–29 and Oct 12 that the dashboard never counted.
3. Tests: 4 new (351 pass). README configuration table and `config/estate.example.env` document the comma list.

Deliberately still "estimate": `deepseek-v4-1-flash` (DeepSeek prices by time of day; off-peak is half), `gpt-5.5` (2x input past 272K; its request sizes could not be verified; about $0.56 in 30 days), `glm-5.3` (0.3M tokens). The reasons are a comment in `prices.py`.

## Limits
- Frozen days are never recomputed. Past luna and kimi days stay labelled "estimate" (luna's dollars already equal the official rate), so October's Models table still shows luna as "estimate"; periods starting 2026-10-16 show "list price". That second folder's past usage (about $70 at list price) is not in the ledger; adding it needs a rebuild under a new `LEDGER_VERSION` (owner decision).
- tokscale stays pinned at 4.14.0 (latest 4.19.1, released 2026-10-15). 4.19.0 reads Codex `.jsonl.zst` and Claude advisor usage. Today the pin is safe: Codex compresses a rollout about 8 days after use, the usage lane freezes days after 2, and tokscale's cache kept compressed sessions. A bump invalidates tokscale's Codex cache and needs the README's one-off `--backfill`, so do it deliberately.
- The collector's own Codex reader (`_modified_since`) also matches only `*.jsonl`. Fine while compression lags the freeze; if Codex starts compressing within 2 days, live days would lose Codex tokens.

## Open owner decisions
- Hub disk is 90% full (7.4 GB free of 75 GB). VictoriaMetrics turns read-only below 3 GB free (`-storage.minFreeDiskSpaceBytes=3GB`), which would freeze the dashboard. Its own data is only 14 MB; the space is twelve tagged Open WebUI images from another service on the same host (production plus rollback tags, 6.5–7 GB each, shared layers) and a 14.3 GB build cache. Nothing large grew in the last 14 days, so the risk is that service's next image build. Pruning its old rollback images is the owner's call; nothing was deleted here (a separate session took it up on 2026-10-15).
- None on git: all three change sets were committed separately and pushed on 2026-10-15 after a public-repo scrub (each commit passes the full suite on its own: 344, 347, 351 tests).

## Next step
Nothing is required. Optional checks: on 2026-10-16, the Models table for "Today" should show gpt-5.6-luna as "list price"; the next time that second config dir is used, that day's claude-code value should include it.
