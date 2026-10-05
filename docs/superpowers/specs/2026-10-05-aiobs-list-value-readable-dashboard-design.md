# AI Estate: correct list-price value + readable dashboard — design

Date: 2026-10-05 · Status: owner-approved design (spec awaiting review) · Repo: `ai-estate-obs`

## 1. Why

Owner report: "the dashboard is getting the month-to-date wrong", wants to pick a month (this / last), asks whether the panels are useful, and wants it "beautiful for a human to read".

The audit on 2026-10-05 found:

| Finding | Evidence |
|---|---|
| October MTD shown $1.69K; list-price value is ≈ $2.7K | Spend MTD card vs an independent recount from raw logs and the official price pages |
| Codex Astra priced at ≈ half the standard API rate, with no Fast/Ultrafast premium | tokscale's implied Astra rate ≈ $5 in / $0.50 cached / $25 out. The OpenAI pricing page (fetched 2026-10-05) lists Standard $10 / $1 / $50, Fast 2×, Ultrafast 6×. 85% of October Astra tokens ran at Fast or Ultrafast (90% of those with known speed). |
| Claude Code 1-hour cache writes priced as 5-minute writes (1.25× instead of 2× input) | Raw transcripts: main-thread writes use `ephemeral_1h_input_tokens`. October Claude Code is $1,289.98 (tokscale) vs $1,381.70 (official). |
| September corrupted both ways | Opus 5.5 / Fable 5.1 recorded +20.0% Sep 19 – Oct 1: tokscale priced them from a reseller row until ≈ Oct 2, and the push filter froze those days. Opus 5 lost 92% of its September tokens and nearly all its cost (Aug 31 – Sep 18 counter-shrink bug; fixed going forward on 09-19, history never repaired). |
| Token counts are right | Claude Code transcripts recount = tokscale to the token for September. Codex speed lane vs tokscale for October Astra: 380.7M vs 380.5M. |
| Repricing hazard is structural | tokscale reprices all history from live third-party price lists on every run. The collector freezes past days and banks drops as monotonic offsets. |
| Panels that mislead or add little | Allowance Index / Credit Scenario / Speed Coverage are estimates; only 28% of their 30-day window has speed data. Month forecast. Cache hit rate (flat ≈ 97%). The local GPU section takes about a third of the page while the box idles at ≈ 17 W (≈ 1M local tokens generated in 30 days). |
| Real limits exist but are unused | Every Codex `token_count` event carries `rate_limits` (weekly `used_percent`, `resets_at`, `credits.balance`, `plan_type`). Claude's `cachedUsageUtilization` in `~/.claude.json` is stale (last fetched 2026-09-28), so it is out of scope. |
| Month picking is impossible today | Header cards and the breakdown table are pinned to the current month and to 30d, so "Previous month" in the time picker changes nothing. |

## 2. Goals and non-goals

Goals
1. Dollar figures mean **API list-price value**: what the usage would cost at the vendors' official API prices, including speed tier and cache TTL. OpenRouter stays **real spend**.
2. Every past day's numbers are stable. Nothing silently reprices or shrinks history again.
3. Any period is one click away: **This month** (default), **Last month**, **Last 30 days**, or any picker range. Every total follows the chosen period.
4. Real Codex limits are shown live: weekly limit %, reset time, credit balance.
5. A calm, readable dashboard: plain-English titles, a few big numbers, consistent provider colours, and low-value sections folded away.

Non-goals
- Actual subscription billing. Plan fees are fixed and not in any log.
- Claude subscription limits. No fresh local source exists.
- Deleting or rewriting existing VictoriaMetrics series.
- Changing the GPU Detail and Inference Detail dashboards.

## 3. Owner decisions (2026-10-05)

- Dollars = list-price value, corrected, with real limits and OpenRouter real spend alongside.
- New series with a clean backfill; old series are kept and nothing is deleted.
- The GPU section and the pipeline/Hermes section fold away at the bottom.
- The Astra allowance-estimate panels are removed in favour of the real limit numbers.
- Commits and pushes still need an explicit go (repo AGENTS.md).

## 4. Data design

### 4.1 Price table — `workstation/aiobs_collector/prices.py`

A pure module, owned by the repo. Each entry gives a model's base $/MTok rates and modifiers, plus `since` (the first local date the entry applies to) and `source` (URL and fetch date).

**Anthropic** (platform.claude.com/docs/en/about-claude/pricing, fetched 2026-10-05):

| Model | In | Out | Cache read |
|---|---|---|---|
| claude-fable-5-1, claude-mythos-5-1 | 10 | 50 | 0.025× |
| claude-opus-5-5 | 4 | 20 | 0.05× |
| claude-sonnet-5-5, claude-sonnet-5 | 2 | 10 | 0.1× |
| claude-haiku-4-5 | 1 | 5 | 0.1× |
| claude-fable-5 | 10 | 50 | 0.1× |
| claude-opus-5, claude-opus-4-8 | 5 | 25 | 0.1× |
| claude-sonnet-4-5 | 3 | 15 | 0.1× |

- Cache writes: 1.25× input (5-minute) and 2× input (1-hour).
- Fast mode: Opus 5.5 $8 / $40; Opus 5 and Opus 4.8 $10 / $50. Cache multipliers stack on top.
- Claude 4.6+ models have no long-context surcharge.
- Sonnet 4.5's > 200K rate must be re-verified live before it is coded. If it cannot be verified, it is omitted and noted.

**OpenAI** (developers.openai.com/api/docs/pricing, fetched 2026-10-05). Rates are in / cached / cache write / out:

| Model | Rates |
|---|---|
| gpt-6-astra | 10 / 1 / 12.50 / 50 |
| gpt-6.1-sol | 2 / 0.10 / 2.50 / 10 |
| gpt-6-luna | 0.10 / 0.01 / 0.125 / 0.50 |
| gpt-5.6-sol | 4 / 0.40 / 5 / 20 |
| gpt-5.3-codex | 1.75 / 0.175 / – / 14 |

- Fast = 2× every category. Ultrafast (Astra only) = 6×.
- Every Codex model here runs with a 258,400-token window. The 272K long-context tier never applies, so it is not modelled.

**Lookup normalisation.** Droid spellings map to these entries: `gpt-5-6-sol` → `gpt-5.6-sol` and `gpt-6-1-sol` → `gpt-6.1-sol`. A `-fast` suffix means fast mode on the base model.

**Fallback.** A model with no entry is valued at tokscale's own cost for that day and is also counted in `aiobs_list_value_fallback_usd_total`. This covers glm, kimi, deepseek, grok, gpt-5.5, gpt-5.4, gpt-5.6-luna, gpt-5.6-terra and codex-auto-review, so the dashboard can say how much of a period is estimated.

**Change rule.** A future price change is a new entry with `since` = its effective date, so past days keep their price. Correcting a past price is a deliberate rebuild under a new metric version, never a silent edit.

### 4.2 Usage lane — `workstation/aiobs_collector/lane_usage.py` (lane name `usage`)

Per local day (the workstation's timezone, the same bucketing as tokscale's `bucketTimezone`), per provider and model:

- **claude-code** comes from the raw transcripts `~/.claude/projects/**/*.jsonl`, subagents included.
  - Deduplicate by `(message.id, requestId)`; skip `<synthetic>`.
  - Kinds: input, output, cache_read, cache_write. `cache_write` is split 5m / 1h for valuation only.
  - `usage.speed == "fast"` selects fast-mode rates.
  - This recount matched tokscale to the token for September, so tokscale's claude rows are not used.
- **codex** uses tokscale `graph` tokens: input, cacheRead, cacheWrite, and output **plus reasoning** (tokscale splits reasoning out; OpenAI bills it as output).
  - Value = Σ over speed and kind of tokscale tokens(day, model, kind) × share(day, model, kind, speed) × rate(kind) × speed multiplier. The share is that speed's fraction of the kind's tokens in the session-log walk for that day and model; with no walk data the share is 100% unknown.
  - Speed shares come from the session logs joined to `logs_2.sqlite` feedback tags. This reuses `lane_codex_speed.read_modes`, the speed lane's cached Astra modes, and the same delta/fingerprint walk, generalised to every Codex model.
  - Unknown speed is valued at Standard, which is a floor. Speed is known from 2026-09-26.
- **droid, grok, cursor, others** use tokscale tokens (output + reasoning) × table, or the fallback.
- **hermes** is excluded, as today. It mixes local and cloud-routed traffic.
- **openrouter** is not emitted. Its real cost stays in the existing `openrouter` lane.

**Day ledger.** Days at or before *today − 2* are computed once, frozen, and stored in lane state (`lane:usage:data`, with a version number). Yesterday and today are recomputed every run, which gives late writes and speed metadata a day of grace.

Cumulative totals = frozen days + live days. History therefore cannot drift from repricing, transcript clean-up, archived Codex sessions or diagnostic-log rotation. The ledger is saved only after a successful push, the same boundary every other lane's state uses. The first run freezes the whole available history: Claude Code from 2026-08-13 and tokscale history from 2026-02-16.

### 4.3 Metric contract (new; old metrics untouched)

| Metric | Labels | Type |
|---|---|---|
| `aiobs_usage_tokens_total` | provider, model, kind ∈ {input, output, cache_read, cache_write}, origin="client" | cumulative, day-end stamped |
| `aiobs_list_value_usd_total` | provider, model, origin="client" | cumulative, day-end stamped |
| `aiobs_list_value_fallback_usd_total` | provider, model, origin="client" | cumulative; the part valued by fallback |
| `aiobs_codex_limit_used_ratio` | window ∈ {weekly, 5h, …} (from `window_minutes`) | gauge 0–1 |
| `aiobs_codex_limit_resets_at_seconds` | window | gauge |
| `aiobs_codex_credits_balance` | – | gauge |
| `aiobs_codex_limit_observed_at_seconds` | – | gauge (age of the newest event) |

**Stamping.** The day-end stamping, sparse emission, push high-water filter and monotonic shaping follow the existing lanes. The three cumulative metrics join `monotonic.CUMULATIVE_METRICS`, and `_lane_for_sample` attributes them to `usage`.

### 4.4 Codex limits lane — `workstation/aiobs_collector/lane_codex_limits.py` (lane `codex-limits`)

- It reads the newest `token_count` events that carry `rate_limits` from the most recent session files.
- It emits the gauges above at now. When `resets_at` has already passed, used is 0 because the window rolled over.
- It also emits one point per 10 minutes of observed history (from event timestamps), so the "limit over time" chart has history from day one.
- Metadata only. No prompt or response content is ever decoded beyond these fields.

## 5. Dashboard design (`hub/grafana/dashboards/estate.json`, uid unchanged)

### 5.1 Time model

- The default range is **This month so far** (`now/M` → `now`).
- Header links (vars kept): **This month**, **Last month** (`now-1M/M` → `now-1M/M`), **Last 30 days**, GPU Detail, Inference Detail.
- **Period** panels follow the range. Each is an instant query with millisecond-exact anchors: `cum @ (${__to} / 1000) − cum @ (${__from} / 1000)`, using the existing `X or (X*0)` fallback.
  - Day-end samples sit at 23:59:59.999. `${__to:date:seconds}` truncates and dropped the last day of a month (verified: Astra Sep read $915.70 instead of $916.65), so whole-second anchors are banned by lint.
- **Live** panels (Today, Codex limit, credits, pipeline) are instant queries pinned to `now()` and local midnight.
- Daily bars keep the verified forward-looking form (`offset -1d`, `interval: 1d`).
- Template variables: Provider and Model. Token kind is dropped.

### 5.2 Layout (24-column grid)

1. **Header cards** (h4):
   - API value (period)
   - Tokens (period)
   - Today (live API value)
   - OpenRouter spend (period, real money)
   - Codex weekly limit (live %, green/amber/red at 70 / 90%)
   - Codex credits (live balance)
   - Pipeline (live: ✓ All up / ✕ n down)
2. **Value by day**: stacked daily API value by provider (w16) and a value-by-provider bar gauge for the period (w8).
3. **Models**: a breakdown table for the period with Provider pill, Model, Tokens, Output, API value, Share and $/1M tokens, ranked by value. Then a daily API value by model chart (top 10, `topk_max`).
4. **Tokens**: daily tokens by provider (w16) and tokens by kind for the period (w8). This explains why cache reads dominate.
5. **Codex**: Astra speed mix for the period (tokens by Standard/Fast/Ultrafast/unknown, w8) and the weekly limit over time (w16).
6. **Local GPU & inference** (collapsed row): serving engine, GPU power, energy for the period, generation throughput, local tokens for the period, and links.
7. **Pipeline & Hermes** (collapsed row): estate timeline, collector lanes (including `usage` and `codex-limits`), and the Hermes client view (unchanged metric).

### 5.3 Visual rules

- Titles are short plain English; there are no "MTD", "Index" or "Scenario" titles. Descriptions are one or two sentences: what is counted, which prices, and the caveats (Astra speed known from Sep 26; Claude Code history from Aug 13; the fallback share).
- Provider colours stay fixed everywhere: claude-code #d95926, codex #3987e5, droid #199e70, grok #c98500, openrouter #9085e9.
- Big cards use whole dollars and tables use cents. Tokens use short units.
- Header cards are header-less KPI cards, as today.

### 5.4 Removed

Allowance Index, Credit Scenario, Speed Coverage, the "How Astra speed affects usage" text, Astra Allowance Index per Day, Month forecast, Cache hit rate, the separate Cost/Tokens MTD bar gauges (replaced by the period gauges), and the Token-kind variable.

## 6. Testing and verification

- **TDD.** Write unit tests first for:
  - price lookups, modifiers and normalisation;
  - the transcript parser (dedup, 5m/1h split, fast, subagents, synthetic);
  - speed shares;
  - the ledger (freezing, grace, version reset);
  - the lane assembly (fixture tokscale doc plus fixtures);
  - the limits parser (reset rollover, missing fields);
  - `_lane_for_sample` and monotonic membership.
- **Lint** (`test_dashboards.py`):
  - New metrics are added to the counter rules (no `increase()`, forward daily bars).
  - Period panels must use `@ (${__from} / 1000)` and `@ (${__to} / 1000)`, and never `:date:seconds` on counters.
  - Live cards must pin `now()`.
  - Each provider keeps one colour; nothing overlaps; every variable is defined.
  - The zoom-proof and MTD-anchor rules are retired for estate and kept for the other dashboards.
- **Live checks before deploy:**
  - A dry run of the new lanes must reproduce the independent recount: October Claude Code $1,381.70 ± 0.5%, and October Astra speed-aware ≈ $1.17K.
  - The September Claude Code value from the ledger must match the recount ($7,502.18).
  - The VM period queries must equal the lane's own sums to the cent for October and September.
- **Preview.** Push the dashboard as uid `aiobs-estate-preview` to the hub directory only, screenshot it at 1920 and 1440 wide, check "Last month" and "This month", then delete the preview.

## 7. Rollout and rollback

1. Add the modules and tests. The collector is unaffected until the lanes are registered and added to `AIOBS_LANES` in the untracked `config/estate.env`.
2. Register the lanes, run a dry run, then run once. The first run backfills about 230 days of new series.
3. Deploy the dashboard by rsync after the preview.
4. Rollback: remove the lanes from `AIOBS_LANES`, `git checkout` the dashboard, and rsync. The old series and the old lanes keep running throughout.
5. Commit and push only on the owner's explicit go.

## 8. Risks and open items

- Unknown Astra speed before 2026-09-26 is valued at Standard, so September Astra is a floor.
- Fallback models (≈ 1% of October value) inherit tokscale's prices.
- If the state file is lost, the ledger rebuilds from whatever the sources still hold; VM keeps higher earlier values at the same stamps.
- A Claude transcript scan costs about 8–10 s per run. A per-file cache is a later optimisation if needed.
- "GPT-5.6 Sol promotional pricing" may end after 2026-11-21. That needs a new table entry then.
