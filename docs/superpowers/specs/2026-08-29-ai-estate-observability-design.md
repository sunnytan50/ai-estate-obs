# AI Estate Observability — Design Spec

- **Date:** 2026-08-29
- **Status:** Approved design (owner: the owner), spec written for review
- **Repo:** `~/Documents/New project/ai-estate-obs/` (working name `ai-estate-obs`; final public name is an owner decision before any publish — nothing hardcodes the name)

## 1. Goal

One Grafana dashboard showing, with history:

1. **Watts** — live and historical power draw (plus utilization, VRAM, temperature, clocks) of the RTX 5090 Windows box.
2. **Local token flow** — tokens/sec, prompt vs generation volume, latency for the LLM server lanes on that box (SGLang and llama.cpp).
3. **Cloud token flow** — daily tokens and cost across coding agents: Claude Code, Codex CLI, Cursor, Factory Droid, Hermes Agent, and OpenRouter.

**Reusability requirement (added 2026-08-29):** the repo must be usable by third parties. Generic code and templates only; every site-specific value (hosts, IPs, key paths, lane toggles) lives in an untracked config file instantiated from a committed `.example`. Acceptance: a stranger with a Docker host, an NVIDIA box, and some supported coding agents can deploy from the README alone.

### Non-goals

- Fixing Windows-box boot residency (tracked separately as D2 in the Hermes performance plan). A full box reboot causes a metrics gap until the existing manual restart ritual runs; WSL-internal restarts are covered by systemd units.
- Whole-wall power (smart plug) — GPU-only was chosen; the schema leaves room to add a plug exporter later.
- Alerting, Sankey flow panel, CodexBar limit windows — explicitly deferred to an optional Phase 3, decided after Phase 2 ships.
- Publishing the repo to GitHub — built public-ready, but pushing/publishing is a separate per-turn owner approval.
- Capturing prompt/response content anywhere. Token counts, timestamps, model names, and cost only.

## 2. Approved decisions

| Decision | Choice |
|---|---|
| Host for Grafana + TSDB | cloud VPS (`my-vps`), Docker Compose at `/opt/observability/` |
| Connectivity | Install Tailscale on the VPS; all metric traffic rides the tailnet. Push-only spokes; nothing new listens off-loopback except on tailnet interfaces |
| TSDB | VictoriaMetrics single-node (Prometheus-compatible, light enough for the 3.7 GiB VPS) |
| Box telemetry depth | GPU-only via `nvidia_gpu_exporter` (nvidia-smi) |
| llama.cpp lanes | Add `--metrics` to the llama.cpp start scripts (approved); SGLang already exports metrics |
| Cloud token parsing | **tokscale** (`junhoyeo/tokscale`) as the engine — covers Claude Code, Codex, Cursor, Droid/Factory, Hermes locally; no hand-written parsers |
| OpenRouter | Official activity/credits API, polled directly (key already present in `~/.hermes/.env`; never committed) |
| Cursor/Factory posture | Best-effort via tokscale; upstream maintains the fragile parts |

## 3. Architecture

```
                 ┌──────────────────────────── cloud VPS (hub) ────────────────────────────┐
                 │  Tailscale (new)                                                          │
                 │  /opt/observability: docker compose                                       │
                 │    victoriametrics :8428  ◄── remote_write / import (tailnet only)        │
                 │    grafana        :3000  ◄── browser over tailnet                         │
                 └───────────▲──────────────────────────────▲───────────────────────────────┘
                             │ tailnet                      │ tailnet
        ┌────────────────────┴─────────────┐   ┌────────────┴──────────────────────────┐
        │ Windows box / WSL2 (spoke, push) │   │ Mac workstation (spoke, push)         │
        │  nvidia_gpu_exporter :9835       │   │  collector.py (launchd, every 10 min) │
        │  vmagent scrapes:                │   │   lane: tokscale --json               │
        │   localhost:8002/metrics (LLM)   │   │    (claude/codex/cursor/droid/hermes) │
        │   localhost:9835/metrics (GPU)   │   │   lane: OpenRouter activity API       │
        │  → remote_write to VPS :8428     │   │  → POST /api/v1/import/prometheus     │
        └──────────────────────────────────┘   └───────────────────────────────────────┘
```

- **Pull happens only on loopback** (vmagent → local endpoints); **push crosses machines** (remote_write / import over tailnet). The LLM server's loopback-only binding is untouched.
- vmagent buffers to disk (`-remoteWrite.tmpDataPath`) so a tailnet or VPS outage loses nothing once connectivity returns.
- The Mac collector timestamps samples explicitly, so late pushes land at the correct time.

## 4. Components

### C1 — Hub (`hub/`)

Docker Compose: `victoriametrics` (retention 13 months, data in a named volume, `-storage.minFreeDiskSpaceBytes` guard) and `grafana` (OSS). Both bind **only** to the VPS's tailnet IP (compose reads it from the instance env file). Grafana admin password comes from `/opt/keys/grafana-admin-password` via env — the `/opt/keys/` bind-mount pattern already standard on this VPS. Provisioned as code: VictoriaMetrics datasource + dashboard provider + dashboard JSONs. Deploy via `scripts/deploy-hub.sh` (rsync + `ssh sudo docker compose up -d`; the ssh user lacks docker-group membership — confirmed).

VPS constraints honored: 3.7 GiB RAM (stack budget ≤ ~600 MB), disk 85 % used / 11 GB free (this metric volume needs ~1–2 GB/yr; the min-free-disk guard stops ingestion before it starves the disk, and Phase 1 acceptance includes a disk headroom check).

### C2 — GPU box (`gpu-box/`)

Two WSL2 systemd units, deployed by `deploy-gpu-box.sh` over ssh (using the documented base64→`wsl.exe`→bash recipe; the Windows landing shell is cmd.exe):

- `nvidia_gpu_exporter` (single Go binary) on `localhost:9835` — watts, utilization, VRAM, temp, clocks; 15 s scrape.
- `vmagent` scraping `localhost:9835` + `localhost:8002/metrics`, remote-writing to the hub with an on-disk buffer.

llama.cpp lanes need `--metrics` (approved). The live scripts under `/opt/llm/console/` are the edit target — **the repo mirror in `local-llm-console` is known to drift from live** (documented gotcha), so implementation enumerates the actual llama.cpp lane scripts on the box (via `vector-modelctl` / `start-*.sh` inspection), edits live, then syncs the mirror in the console repo. That edit lives in the *console* repo, not this one. Never restart or switch a lane while a generation is active (standing rule).

Server metric prefixes differ by lane (`sglang:*` vs llama.cpp's exposition); dashboards handle both (see C4). SGLang's published Grafana dashboard has a known metric-naming drift issue — panel queries are verified against the live `/metrics` output, not trusted blindly.

### C3 — Workstation collector (`workstation/`)

`collector.py` (Python 3.12, stdlib only), run by launchd every 10 min (`.plist` template; README documents a cron/systemd-timer alternative for Linux users). Lanes are isolated — one failing never kills the others — and each emits `aiobs_lane_up{lane}` 0/1 plus `aiobs_lane_last_success_timestamp{lane}`:

- **tokscale lane:** runs `tokscale --json` (and its daily/graph export), normalizes per client+model+day into the metric schema. Covers Claude Code, Codex, Cursor, Droid/Factory, Hermes in one dependency. Factory's local layout showed `.jsonl`/`.json` sessions but none of the expected `settings.json` — whether tokscale picks it up is verified at implementation; if empty, the lane reports zero rather than failing.
- **OpenRouter lane:** official activity/credits API; key read at runtime from an env file named in the instance config (owner's: `~/.hermes/.env`). Daily tokens + spend by model.

Output: Prometheus text exposition with **explicit timestamps**, POSTed to VictoriaMetrics `/api/v1/import/prometheus` over the tailnet. Writes are idempotent (same series+timestamp = same sample), so re-pushing a day is safe — that idempotency is also the backfill mechanism: `--backfill` mode replays tokscale's full daily history (months of Claude/Codex data) once, at day granularity.

State: a small JSON state file (last pushed day per lane) under `~/.local/state/aiobs/`, only to avoid re-pushing unchanged history every 10 minutes.

### C4 — Dashboards (`hub/grafana/dashboards/`)

One flagship **Estate** dashboard, dark theme, four rows:

1. **Power** — live watts gauge, 24 h watts curve overlaid with tok/s (dual axis), kWh/day (`avg_over_time(power)[1d] × 24 / 1000`), VRAM, temperature.
2. **Local inference** — tok/s, prompt vs generation volume by model lane, TTFT/latency percentiles, request rate. Queries written per lane prefix (`sglang:*`, llama.cpp's) and unioned.
3. **Cloud tokens** — stacked daily tokens by provider, cost/day, month-to-date totals, model breakdown.
4. **Totals** — local vs cloud share, all-estate cumulative counter, collector lane health.

Plus two seeded supporting dashboards: GPU detail (adapted from published nvidia_gpu_exporter dashboards 14574/25547) and inference-server detail (adapted from SGLang's official dashboard, queries verified per the naming-drift caveat). All provisioned from JSON in the repo — no click-built panels.

## 5. Metric schema

| Metric | Labels | Source |
|---|---|---|
| `nvidia_smi_*` (exporter defaults, e.g. `nvidia_smi_power_draw_watts`) | `host` | GPU exporter |
| `sglang:*` / llama.cpp exposition | `model_name`, `host` | LLM server, via vmagent |
| `aiobs_tokens_total` | `provider`, `model`, `kind` (input/output/cache_read/cache_write), `origin` | collector (counter, day-granular history + 10-min live) |
| `aiobs_cost_usd_total` | `provider`, `model`, `origin` | collector, where derivable |
| `aiobs_lane_up`, `aiobs_lane_last_success_timestamp` | `lane` | collector self-health |

**Anti-double-count rule:** local-lane totals come from the *server-side* metrics (authoritative — they see every client). Client-side series (tokscale's Hermes lane, which includes traffic to the local server and to OpenRouter through Hermes profiles) carry `origin="client"` and appear only in per-profile attribution panels, never summed with `origin="server"` series or with the direct OpenRouter lane in the same panel.

## 6. Security

- Nothing public: hub services bind to the tailnet IP only; spokes only push outward.
- No secrets in the repo, ever: keys are read at runtime from paths named in the untracked instance config (`config/estate.env`, instantiated from the committed `config/estate.example.env`, which contains placeholders only). `.gitignore` covers `config/estate.env`, `private/`, state files.
- Grafana admin password in `/opt/keys/` on the VPS (chmod 600), referenced by env — never in compose files.
- MIT license; README states the privacy posture (token counts and timestamps only).

## 7. Failure modes

| Failure | Behavior |
|---|---|
| Windows box reboots | Metrics gap (accepted non-goal); WSL units auto-restart within a running WSL |
| Tailnet/VPS briefly down | vmagent disk buffer replays; Mac collector retries next cycle with explicit timestamps |
| tokscale upstream breaks a provider | That lane's `aiobs_lane_up`=0; other lanes unaffected; version pinned, bumped deliberately |
| Model lane switch on the box | vmagent targets are constant (`:8002`); metric prefix changes are handled in dashboard queries |
| VPS disk pressure (85 % used) | VM retention 13 mo + min-free-disk guard; Phase 1 acceptance includes headroom check |
| Reboot of the VPS | `restart: unless-stopped` on both containers; Tailscale as a systemd service |

## 8. Phases & acceptance

- **P1 — watts + local tokens live.** Tailscale on VPS; hub stack up; box exporters running. *Accept:* fire one generation through `:18002`; watch watts spike and server token counters move on the live dashboard; VPS disk headroom re-checked after 24 h of ingestion.
- **P2 — cloud tokens + backfill.** Collector live with tokscale + OpenRouter lanes; one-time backfill lands months of history. *Accept:* dashboard daily totals match `tokscale` TUI for the same day (± rounding) and OpenRouter's own dashboard for a sample day; lane-health panel green.
- **P3 (optional, decided later)** — alerting, Sankey, CodexBar limit windows, README screenshots for the public.

Every phase ends with evidence on the live dashboard, not assertions.

## 9. Repo layout

```
ai-estate-obs/
├── LICENSE                     MIT
├── README.md                   quickstart for third parties (hub / gpu-box / workstation)
├── .gitignore                  estate.env, private/, state, .firecrawl
├── config/estate.example.env   ALL site-specific values, placeholders only
├── hub/                        docker-compose.yml, grafana provisioning + dashboard JSONs
├── gpu-box/                    systemd unit + vmagent config templates, deploy-gpu-box.sh
├── workstation/                collector.py, lanes/, launchd plist template, installer
├── scripts/deploy-hub.sh
└── docs/superpowers/{specs,plans}/
```

The owner's filled-in `config/estate.env` (VPS host, tailnet IPs, key file paths, lane toggles) stays untracked; `private/` holds any operator notes that shouldn't ship.
