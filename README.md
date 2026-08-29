# AI Estate Observability

One Grafana dashboard showing live and historical power draw (watts, utilization, VRAM, temperature) of your GPU box, local token flow (tokens/sec, latency) across LLM server lanes, and cloud token flow (daily spend and token counts) across all coding agents — Claude Code, Codex CLI, Cursor, Factory Droid, Hermes Agent, and OpenRouter — in one unified view.

## Architecture

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

All three machines — hub, GPU box, workstation — join the **same tailnet**. Nothing in this stack listens on a public interface: the hub's services bind only to its tailnet IP, and both spokes only ever push outward.

## Prerequisites

- **A Docker host** you can `ssh` into with passwordless sudo (the "hub" — a small VPS is plenty; the stack budgets under ~600 MB RAM and ~1-2 GB/yr of disk for a personal-scale estate).
- **A [Tailscale](https://tailscale.com) account** (the free tier covers this). You'll install and authenticate Tailscale on all three machines below.
- **An NVIDIA GPU box** (Linux, or Windows with WSL2) running an inference server that exposes Prometheus metrics on loopback — SGLang (metrics on by default) or llama.cpp's `llama-server` started with `--metrics`. Reachable via `ssh` from wherever you run the deploy scripts.
- **A workstation** (macOS or Linux) running one or more supported coding agents (Claude Code, Codex CLI, Cursor, Factory Droid, Hermes Agent) with `python3` (3.9+) and Node.js/npm (`npx` on `PATH` — used to run the pinned [`tokscale`](https://github.com/junhoyeo/tokscale) CLI; no specific Node version is pinned, only the `tokscale` package version itself).
- `ssh` config aliases (in `~/.ssh/config`) for the hub and the GPU box, so `ssh $AIOBS_HUB_SSH_HOST` / `ssh $AIOBS_BOX_SSH_HOST` just work from your operator machine.
- **(Optional)** an [OpenRouter](https://openrouter.ai) account, if you want direct OpenRouter spend tracked as its own lane — see Step 4 below.

## Quickstart

Every site-specific value (hosts, IPs, key paths, which lanes to run) lives in one untracked file, `config/estate.env`, instantiated from the committed `config/estate.example.env`. Nothing site-specific is ever committed.

```bash
git clone <this-repo> && cd ai-estate-obs
cp config/estate.example.env config/estate.env
```

Open `config/estate.env` in an editor and work through the steps below in order — each step fills in the vars that step needs. The full var-by-var reference is in [Configuration reference](#configuration-reference).

### 1. Hub

1. Install Tailscale on the hub and bring it up (`tailscale up`, authenticate in the browser link it prints). Get its tailnet IPv4 with `tailscale ip -4` and put it in `AIOBS_HUB_TAILNET_IP`.
2. Fill in `AIOBS_HUB_SSH_HOST` (your `ssh` alias for the hub) and, if you want non-default ports/retention/versions, the rest of the hub block.
3. Create the Grafana admin secret **on the hub** — the deploy script asserts this file exists and is non-empty, then locks its permissions down; it does not generate the password for you:
   ```bash
   ssh "$AIOBS_HUB_SSH_HOST" 'sudo mkdir -p /opt/keys && \
     openssl rand -base64 24 | sudo tee /opt/keys/grafana-admin-password >/dev/null'
   ```
   (Adjust the path if you changed `AIOBS_GRAFANA_ADMIN_PASSWORD_FILE` from its default.)
4. Deploy:
   ```bash
   bash scripts/deploy-hub.sh
   ```
   This rsyncs `hub/` to `/opt/observability` on the hub, copies `config/estate.env` there as `.env`, verifies + tightens the admin-password file's permissions, and runs `docker compose up -d`.
5. Confirm: `http://<hub-tailnet-ip>:3000` over the tailnet should show a Grafana login page. Log in as `admin` / the password you generated in step 3.

### 2. GPU box

1. Install Tailscale on the GPU box (inside the WSL2 distro, if that's your setup) and join the same tailnet.
2. Fill in `AIOBS_BOX_SSH_HOST`, `AIOBS_BOX_HOST_LABEL` (the `host` label stamped on every metric from this box), `AIOBS_LLM_METRICS_TARGET` (your inference server's loopback `host:port`, e.g. `127.0.0.1:8002`), and `AIOBS_GPU_EXPORTER_PORT`. Set `AIOBS_BOX_WSL_DISTRO` to your WSL distro name, or leave it empty for a plain Linux box (no `wsl.exe` wrapper is used).
3. Make sure your inference server actually exposes `/metrics`: SGLang does by default; for llama.cpp, add `--metrics` to the `llama-server` invocation.
4. Deploy:
   ```bash
   bash gpu-box/deploy-gpu-box.sh
   ```
   This renders two systemd units (`nvidia_gpu_exporter`, `vmagent`) and a scrape config locally from the `.tmpl` files, then ships the *rendered, concrete* files to the box over `ssh` (base64-piped through `wsl.exe` when `AIOBS_BOX_WSL_DISTRO` is set, plain `ssh` otherwise) and installs them. `nvidia_gpu_exporter` binds loopback-only; `vmagent` scrapes both exporters every 15s and remote-writes to the hub, buffering to disk if the tailnet is briefly down.
5. Confirm: within a minute or two, `nvidia_smi_power_draw_watts` and your server's own metric names should be queryable at `http://<hub-tailnet-ip>:8428/api/v1/query?query=up` over the tailnet.

### 3. Workstation collector

1. Install Tailscale on the workstation too (or otherwise ensure it can reach the hub's tailnet IP).
2. Fill in `AIOBS_LANES` (comma list — `tokscale`, `openrouter`, or both; an unknown lane name is a hard config error), `AIOBS_TOKSCALE_VERSION` (check `npm view tokscale version` for current), and `AIOBS_STATE_DIR`.
3. **(Optional) OpenRouter lane:** create a *Management API Key* (not a regular completion key — OpenRouter treats these as separate credential classes) at [openrouter.ai/settings/management-keys](https://openrouter.ai/settings/management-keys). Put it in a plain `KEY=VALUE` file (same format as `estate.env` itself) at the path named by `AIOBS_OPENROUTER_ENV_FILE`, under the var name in `AIOBS_OPENROUTER_KEY_NAME` (default `OPENROUTER_MANAGEMENT_KEY`) — e.g. a line reading `OPENROUTER_MANAGEMENT_KEY=sk-or-...` in `~/.hermes/.env`. This key is read at runtime and **never copied into this repo**. Skipping this is fine: the lane simply stays fail-closed (`aiobs_lane_up{lane="openrouter"}` reads `0`) until you add it — see [Troubleshooting](#troubleshooting).
4. Install the collector as a scheduled job:
   - **macOS:**
     ```bash
     bash workstation/install-collector.sh
     ```
     Installs a launchd user agent (`com.aiobs.collector`) that runs once immediately, then every 600s, logging to `~/Library/Logs/aiobs/collector.log`.
   - **Linux:** no installer script ships for this yet — use cron or a systemd user timer calling the same entry point on the same ~600s cadence. Cron:
     ```cron
     */10 * * * * cd /path/to/ai-estate-obs/workstation && /usr/bin/python3 -m aiobs_collector --config /path/to/ai-estate-obs/config/estate.env >> "$HOME/.local/state/aiobs/collector.log" 2>&1
     ```
     or a systemd user timer (`~/.config/systemd/user/aiobs-collector.{service,timer}`):
     ```ini
     [Service]
     Type=oneshot
     WorkingDirectory=/path/to/ai-estate-obs/workstation
     ExecStart=/usr/bin/python3 -m aiobs_collector --config /path/to/ai-estate-obs/config/estate.env
     ```
     ```ini
     [Timer]
     OnBootSec=1min
     OnUnitActiveSec=600
     Persistent=true
     [Install]
     WantedBy=timers.target
     ```
     then `systemctl --user enable --now aiobs-collector.timer`.
5. Seed history once (tokscale re-walks its full local transcript history, so this can take a while on a large history — it's a one-time cost):
   ```bash
   cd workstation && python3 -m aiobs_collector --config ../config/estate.env --backfill
   ```
   Every subsequent run (the launchd/cron/timer cadence) only pushes what's new, via a small per-lane high-water-mark kept in `AIOBS_STATE_DIR`.

### 4. Look at it

`http://<hub-tailnet-ip>:3000/d/aiobs-estate` over the tailnet — the **AI Estate** dashboard: Power, Local Inference, Cloud Tokens, and Totals & Health rows.

## Configuration reference

Every variable lives in `config/estate.env` (copy of `config/estate.example.env` with real values). None of it is ever committed.

| Variable | Used by | Meaning |
|---|---|---|
| `AIOBS_HUB_SSH_HOST` | `deploy-hub.sh` | `ssh` alias/host for the hub |
| `AIOBS_HUB_TAILNET_IP` | hub, gpu-box, workstation | Hub's tailnet IPv4 (`tailscale ip -4` on the hub) — everything pushes/queries here |
| `AIOBS_VM_PORT` | hub, gpu-box, workstation | VictoriaMetrics port (default `8428`) |
| `AIOBS_GRAFANA_PORT` | hub | Grafana port (default `3000`) |
| `AIOBS_VM_RETENTION_MONTHS` | hub | VictoriaMetrics data retention |
| `AIOBS_VM_VERSION` | hub, gpu-box | Pinned VictoriaMetrics release (hub's Docker image tag; gpu-box's `vmagent` binary reuses the same tag) |
| `AIOBS_GRAFANA_VERSION` | hub | Pinned Grafana OSS Docker image tag |
| `AIOBS_GRAFANA_ADMIN_PASSWORD_FILE` | hub | Path **on the hub** to the admin-password secret. The operator creates this file's *content* (see Quickstart Step 1.3); `deploy-hub.sh` only asserts it's non-empty and tightens it to mode `640` (group-readable, since the non-root Grafana container reads it via `GF_SECURITY_ADMIN_PASSWORD__FILE`) |
| `AIOBS_BOX_SSH_HOST` | `deploy-gpu-box.sh` | `ssh` alias/host for the GPU box |
| `AIOBS_BOX_WSL_DISTRO` | `deploy-gpu-box.sh` | WSL2 distro name if the box is Windows; **empty** for a plain Linux box (skips the `wsl.exe` wrapper entirely) |
| `AIOBS_BOX_HOST_LABEL` | gpu-box | `host` label value stamped on every metric from this box |
| `AIOBS_LLM_METRICS_TARGET` | gpu-box | Loopback `host:port` of the inference server's `/metrics` on the box (e.g. `127.0.0.1:8002`) |
| `AIOBS_GPU_EXPORTER_PORT` | gpu-box | `nvidia_gpu_exporter`'s loopback listen port |
| `AIOBS_LANES` | workstation | Comma list of collector lanes to run: `tokscale`, `openrouter`, or both. An unrecognized name is a hard config error (exit 2), before any network call |
| `AIOBS_TOKSCALE_VERSION` | workstation | Pinned `tokscale` npm package version (`npx -y tokscale@<version>`) — check `npm view tokscale version` for current |
| `AIOBS_OPENROUTER_ENV_FILE` | workstation | Path to a file containing the OpenRouter *Management* API key. Read at runtime, never copied into this repo |
| `AIOBS_OPENROUTER_KEY_NAME` | workstation | The var name inside that file holding the key (default `OPENROUTER_MANAGEMENT_KEY`) |
| `AIOBS_STATE_DIR` | workstation | Where the collector keeps its small JSON push-state file (per-lane high-water mark, mode `0700`) |

## Privacy

This system captures token counts, timestamps, model names, and cost only — **never prompt or response content**. The collector's two data metrics (`aiobs_tokens_total`, `aiobs_cost_usd_total`) carry only `provider`/`model`/`kind`/`origin` labels; nothing else is read from your agent transcripts or sent anywhere.

## Troubleshooting

- **`AIOBS_HUB_TAILNET_IP unset` from `deploy-hub.sh`.** Run the Tailscale setup on the hub first (Quickstart Step 1.1) and copy its real tailnet IP into `config/estate.env` — the deploy script refuses to proceed with the placeholder.
- **Grafana falls back to `admin`/`admin`.** The admin-password secret file on the hub is missing, empty, or not group-readable by the Grafana container's user. Re-run the Step 1.3 recipe and re-deploy; `deploy-hub.sh` asserts the file is non-empty and chmods it to `640` on every run.
- **WSL2 box: `nvidia-smi` not found / exporter won't start.** WSL2 keeps `nvidia-smi` under `/usr/lib/wsl/lib`, which is on an interactive login `PATH` but **not** on systemd's or `sudo`'s default `PATH` — `deploy-gpu-box.sh`'s installer detects this and symlinks it onto `/usr/local/bin` automatically (a harmless no-op on a plain Linux box where `nvidia-smi` is already reachable). If the exporter still won't start, confirm `nvidia-smi` genuinely works inside the WSL distro first (`wsl -d <distro> -e nvidia-smi`).
- **WSL2 box: nothing seems to deploy / systemd unit calls fail oddly.** Some WSL2 configurations ship with systemd disabled by default (`systemd=true` needs to be set under `[boot]` in `/etc/wsl.conf`, followed by `wsl --shutdown` and a restart of the distro). `deploy-gpu-box.sh` installs and enables two systemd units — if `systemctl` isn't available inside the distro, this is almost always why.
- **`aiobs_lane_up{lane="openrouter"} == 0`.** Expected until you provision an OpenRouter *Management* API Key (Quickstart Step 3.3) — this is a fail-closed design, not a bug. Regular OpenRouter completion keys are a different credential class and will not work here; OpenRouter's own API rejects them for this endpoint with 401/403.
- **`aiobs_lane_up{lane="tokscale"} == 0`.** Check the collector's log (`~/Library/Logs/aiobs/collector.log` on macOS, or your cron/systemd redirect target on Linux) for the actual exception — common causes are `AIOBS_TOKSCALE_VERSION` pointing at a version that no longer exists on npm, or `npx` not being on the `PATH` launchd/cron/systemd actually runs with (the launchd plist explicitly adds the `npx` directory to its own minimal `PATH` for exactly this reason).
- **A lane's `aiobs_lane_last_success_timestamp` series is entirely absent, not zero.** By design — that metric is only ever emitted once a lane has *actually* succeeded at least once. "Absent" and "zero" are deliberately different signals here: absent means "never succeeded," a real zero would mean "succeeded exactly at the Unix epoch."
- **Extending the dashboards: don't reach for `increase()` on `aiobs_tokens_total` / `aiobs_cost_usd_total`.** These are cumulative counters pushed at roughly one point per calendar day (plus a live point for "today"). That shape — long, roughly day-spaced gaps between samples as the norm rather than the exception — sits squarely in the case VictoriaMetrics' `increase()` counter-reset/staleness heuristic is not tuned for: verified live against this project's own real data, `increase(aiobs_tokens_total{...}[1d])` returned wildly different (both over- and under-counting, by as much as ~27x) results depending on window size and the exact instant it was evaluated at, with no window size found that was reliably correct. Every panel on this dashboard that reads these two metrics instead uses a direct two-point subtraction — `max_over_time(metric[W]) - (max_over_time(metric[W] offset D) or (max_over_time(metric[W])*0))` — verified byte-exact against a raw `/api/v1/export` ground-truth cross-check. Two details matter if you copy this pattern: (1) use `X or (X*0)` as the zero-fallback, **not** `X or vector(0)` — the latter has no labels, so on a grouped/multi-series query it silently *drops* (rather than zero-fills) any series that lacks an exact label match on the offset side; caught live on a per-model breakdown that came back missing 6 of 7 models before the fix. (2) For `stat`/instant-style panels, issue the query as a **range** query reduced to its last value (`instant: false, range: true`, `reduceOptions.calcs: ["lastNotNull"]`), not as a genuine instant query (`instant: true, range: false`) — verified live that VictoriaMetrics' instant-query path can return a materially wrong answer for this same `max_over_time(...) offset <large duration>` shape at an evaluation instant where the equivalent range query gets it exactly right; this project's Month-to-Date, Model Breakdown, and Local vs Cloud Share panels all route around it this way. Separately, the two `aiobs_lane_*` health metrics are plain gauges with no counter arithmetic at all, but a **bare** instant selector on them can still intermittently return no data at all: Prometheus/VictoriaMetrics apply a short (~5 minute) default staleness lookback to an un-windowed instant query, which is shorter than this collector's 600s push cadence. Wrap them in `last_over_time(metric[20m])` (as the shipped Collector Lane Up / Freshness panels do) rather than querying the bare metric name.
- **A GPU box running llama.cpp:** this project's own reference box currently runs SGLang, so `sglang:*` metric names are live-verified end-to-end; the `llamacpp_*` names used in dashboard queries (`llamacpp_tokens_predicted_total`, `llamacpp_prompt_tokens_total`) match `llama-server --metrics`'s documented exposition but have **not** been independently confirmed against a live llama.cpp lane by this project. If your panels come up empty on the local-inference rows, check your server's actual `/metrics` output first.

## License

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
