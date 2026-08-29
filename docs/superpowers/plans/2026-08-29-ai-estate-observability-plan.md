# AI Estate Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One Grafana dashboard (VPS-hosted, tailnet-only) showing RTX 5090 wattage + local LLM token flow + cloud coding-agent token flow, packaged so third parties can deploy it from the README.

**Architecture:** Hub (VictoriaMetrics + Grafana, Docker Compose on the cloud VPS, bound to its Tailscale IP) fed by two push-only spokes: a WSL2 vmagent on the Windows box (scrapes local GPU exporter + LLM server on loopback, remote-writes out) and a Mac collector (tokscale + OpenRouter lanes, POSTs timestamped samples).

**Tech Stack:** VictoriaMetrics, Grafana OSS, vmagent, nvidia_gpu_exporter, Tailscale, Python 3.12 stdlib (collector), tokscale (npx), launchd + WSL systemd.

**Spec:** `docs/superpowers/specs/2026-08-29-ai-estate-observability-design.md` — read it first; every task argues from it.

## Global Constraints

- **No secrets or site-specific values in tracked files.** Everything site-specific comes from untracked `config/estate.env` (template: `config/estate.example.env` with placeholders only). Keys are read at runtime from files *named* in that env, never copied.
- Hub services bind **only** to the VPS tailnet IP. Spokes never listen off-loopback; they push outward.
- Collector is **Python 3.12 stdlib only** (`/Library/Frameworks/Python.framework/Versions/3.12/bin/python3` on the owner's Mac); tests use `unittest`.
- Metric names exactly as in spec §5: `aiobs_tokens_total{provider,model,kind,origin}`, `aiobs_cost_usd_total{provider,model,origin}`, `aiobs_lane_up{lane}`, `aiobs_lane_last_success_timestamp{lane}`. `kind ∈ {input,output,cache_read,cache_write}`; `origin ∈ {client,server}`.
- Anti-double-count rule (spec §5): panels never sum `origin="client"` with `origin="server"`, and never sum tokscale's Hermes lane with the direct OpenRouter lane.
- Never restart/switch an LLM lane on the box while a generation is active. Never run `vector-modelctl` without the owner's go-ahead.
- Git: local commits only. No push, no repo creation on any forge — publishing is a separate owner approval.
- On the box, the landing shell is cmd.exe: run WSL commands as `ssh my-gpu-box "wsl -d Ubuntu-24.04 -e bash -lc '<cmd>'"`; for scripts, base64 → decode inside WSL (memory recipe).
- License MIT; README documents privacy posture (token counts/timestamps only, no content).

---

### Task 1: Repo scaffolding + instance config contract

**Files:**
- Create: `LICENSE`, `.gitignore`, `README.md`, `config/estate.example.env`
- Already present: `docs/superpowers/specs/…design.md`, this plan

**Interfaces:**
- Produces: the `AIOBS_*` env contract every later task reads. Deploy scripts source `config/estate.env`; collector reads the same file via `--config` path.

- [ ] **Step 1: Write `.gitignore`**

```gitignore
config/estate.env
private/
*.pyc
__pycache__/
.firecrawl/
*.state.json
```

- [ ] **Step 2: Write `LICENSE`** — standard MIT text, copyright line: `Copyright (c) 2026 ai-estate-obs contributors`.

- [ ] **Step 3: Write `config/estate.example.env`** (placeholders only — this is the whole site-specific surface):

```bash
# ---- hub (VPS or any always-on Docker host) ----
AIOBS_HUB_SSH_HOST=my-vps                 # ssh alias/host for deploys
AIOBS_HUB_TAILNET_IP=100.64.0.1           # `tailscale ip -4` on the hub, after Task 3
AIOBS_VM_PORT=8428
AIOBS_GRAFANA_PORT=3000
AIOBS_VM_RETENTION_MONTHS=13
AIOBS_VM_VERSION=v1.111.0                 # pin: verify current stable in Task 2
AIOBS_GRAFANA_VERSION=11.6.0              # pin: verify current stable in Task 2
AIOBS_GRAFANA_ADMIN_PASSWORD_FILE=/opt/keys/grafana-admin-password   # path ON the hub
# ---- gpu box (any NVIDIA box; WSL2 supported) ----
AIOBS_BOX_SSH_HOST=my-gpu-box
AIOBS_BOX_WSL_DISTRO=Ubuntu-24.04         # empty = plain Linux box, no wsl.exe wrapper
AIOBS_BOX_HOST_LABEL=gpubox               # `host` label on box metrics
AIOBS_LLM_METRICS_TARGET=127.0.0.1:8002   # inference server /metrics (loopback on the box)
AIOBS_GPU_EXPORTER_PORT=9835
# ---- workstation collector ----
AIOBS_LANES=tokscale,openrouter           # comma list; unknown names = config error
AIOBS_TOKSCALE_VERSION=                   # pin: filled in Task 9
AIOBS_OPENROUTER_ENV_FILE=~/.hermes/.env  # file containing the key (never copied)
AIOBS_OPENROUTER_KEY_NAME=OPENROUTER_API_KEY
AIOBS_STATE_DIR=~/.local/state/aiobs
```

- [ ] **Step 4: Write `README.md` stub** — title, one-paragraph pitch (watts + local + cloud tokens in one Grafana), architecture ASCII from spec §3, three-component quickstart headings (Hub / GPU box / Workstation) each saying "see Task-N docs, filled in by final task", privacy posture line, MIT badge line. The full quickstart is finalized in Task 12.

- [ ] **Step 5: Owner instance file (untracked):** copy example → `config/estate.env`; fill `AIOBS_HUB_SSH_HOST=my-vps`, `AIOBS_BOX_SSH_HOST=my-gpu-box`, `AIOBS_BOX_HOST_LABEL=win5090`, leave `AIOBS_HUB_TAILNET_IP` for Task 3. Verify: `git status` shows `config/estate.env` **untracked-ignored**.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "chore: scaffold repo — MIT, gitignore, estate.env contract, README stub, spec+plan"
```

---

### Task 2: Hub compose + Grafana provisioning (in-repo, not yet deployed)

**Files:**
- Create: `hub/docker-compose.yml`, `hub/grafana/provisioning/datasources/vm.yml`, `hub/grafana/provisioning/dashboards/provider.yml`, `scripts/deploy-hub.sh`

**Interfaces:**
- Consumes: `AIOBS_*` from Task 1.
- Produces: hub endpoints `http://$AIOBS_HUB_TAILNET_IP:8428` (VM) and `:3000` (Grafana); `deploy-hub.sh` used by Tasks 4, 7, 12.

- [ ] **Step 1: Verify current stable versions** (action, not guesswork):

```bash
curl -s https://api.github.com/repos/VictoriaMetrics/VictoriaMetrics/releases/latest | jq -r .tag_name
curl -s https://api.github.com/repos/grafana/grafana/releases/latest | jq -r .tag_name
```
Write the results into `config/estate.env` (`AIOBS_VM_VERSION`, `AIOBS_GRAFANA_VERSION`) and as the defaults in `estate.example.env`.

- [ ] **Step 2: Write `hub/docker-compose.yml`**

```yaml
services:
  victoriametrics:
    image: victoriametrics/victoria-metrics:${AIOBS_VM_VERSION}
    command:
      - "-retentionPeriod=${AIOBS_VM_RETENTION_MONTHS}"
      - "-storage.minFreeDiskSpaceBytes=3GB"
      - "-httpListenAddr=:8428"
    ports: ["${AIOBS_HUB_TAILNET_IP}:${AIOBS_VM_PORT}:8428"]
    volumes: [vmdata:/victoria-metrics-data]
    restart: unless-stopped
  grafana:
    image: grafana/grafana-oss:${AIOBS_GRAFANA_VERSION}
    ports: ["${AIOBS_HUB_TAILNET_IP}:${AIOBS_GRAFANA_PORT}:3000"]
    environment:
      GF_SECURITY_ADMIN_PASSWORD__FILE: /run/secrets/grafana-admin
      GF_ANALYTICS_REPORTING_ENABLED: "false"
    volumes:
      - grafanadata:/var/lib/grafana
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
      - ./grafana/dashboards:/var/lib/grafana/dashboards:ro
    secrets: [grafana-admin]
    restart: unless-stopped
secrets:
  grafana-admin:
    file: ${AIOBS_GRAFANA_ADMIN_PASSWORD_FILE}
volumes: { vmdata: {}, grafanadata: {} }
```

- [ ] **Step 3: Write provisioning files**

`hub/grafana/provisioning/datasources/vm.yml`:
```yaml
apiVersion: 1
datasources:
  - name: VictoriaMetrics
    uid: vm
    type: prometheus
    access: proxy
    url: http://victoriametrics:8428
    isDefault: true
```
`hub/grafana/provisioning/dashboards/provider.yml`:
```yaml
apiVersion: 1
providers:
  - name: aiobs
    folder: AI Estate
    type: file
    options: { path: /var/lib/grafana/dashboards }
```

- [ ] **Step 4: Write `scripts/deploy-hub.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."; source config/estate.env
[ -n "${AIOBS_HUB_TAILNET_IP}" ] || { echo "AIOBS_HUB_TAILNET_IP unset (run Tailscale task first)"; exit 1; }
ssh "$AIOBS_HUB_SSH_HOST" "sudo mkdir -p /opt/observability && sudo chown \$(whoami) /opt/observability"
rsync -az --delete hub/ "$AIOBS_HUB_SSH_HOST:/opt/observability/"
scp config/estate.env "$AIOBS_HUB_SSH_HOST:/opt/observability/.env"
ssh "$AIOBS_HUB_SSH_HOST" "sudo test -s ${AIOBS_GRAFANA_ADMIN_PASSWORD_FILE} || { echo 'missing admin password file'; exit 1; }"
ssh "$AIOBS_HUB_SSH_HOST" "cd /opt/observability && sudo docker compose --env-file .env up -d --remove-orphans"
```
`chmod +x scripts/deploy-hub.sh`.

- [ ] **Step 5: Syntax-verify without Docker locally**

```bash
python3 -c "import yaml,sys; yaml.safe_load(open('hub/docker-compose.yml')); print('ok')" \
  || /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -c "import yaml,sys; yaml.safe_load(open('hub/docker-compose.yml')); print('ok')"
bash -n scripts/deploy-hub.sh && echo "sh ok"
```
Expected: `ok` + `sh ok` (if PyYAML is absent in both interpreters, fall back to `ruby -ryaml -e "YAML.load_file('hub/docker-compose.yml'); puts 'ok'"` — macOS ships ruby).

- [ ] **Step 6: Commit** — `git add hub scripts config/estate.example.env && git commit -m "feat(hub): compose + provisioning + deploy script"`

---

### Task 3: Tailscale on the VPS (owner-approved system change)

**Files:** none in repo (VPS system state); update `config/estate.env` (`AIOBS_HUB_TAILNET_IP`).

**Interfaces:**
- Produces: reachable tailnet IP for the hub; `net.ipv4.ip_nonlocal_bind=1` so Docker can bind the tailnet IP even if Tailscale comes up after Docker on reboot.

- [ ] **Step 1: Preflight** — `ssh my-vps "df -h / | tail -1; free -h | head -2"`; confirm ≥2 GB disk free before installing anything.
- [ ] **Step 2: Install** — `ssh my-vps "curl -fsSL https://tailscale.com/install.sh | sudo sh"`.
- [ ] **Step 3: Bring up** — `ssh my-vps "sudo tailscale up --ssh=false"` prints an auth URL; **hand the URL to the owner to authorize in the browser** (device auth is an account action — never done autonomously). Wait for confirmation.
- [ ] **Step 4: Nonlocal bind guard** — `ssh my-vps "echo 'net.ipv4.ip_nonlocal_bind=1' | sudo tee /etc/sysctl.d/99-aiobs.conf && sudo sysctl --system | grep nonlocal"`.
- [ ] **Step 5: Verify from the Mac** — `ssh my-vps "tailscale ip -4"` → write result into `config/estate.env` `AIOBS_HUB_TAILNET_IP`; then from Mac: `ping -c 2 <that IP>` and `tailscale status | grep remy` (Mac side shows the new peer). Expected: replies, peer listed.
- [ ] **Step 6: Record** — no repo commit (env is untracked); note the IP in the task log.

---### Task 4: Deploy hub, verify over tailnet

**Files:** none new (uses Task 2 artifacts).

**Interfaces:**
- Produces: live VM at `http://$AIOBS_HUB_TAILNET_IP:8428` (`/health` = OK) and Grafana at `:3000` (datasource green). Later tasks push/query these.

- [ ] **Step 1: Create the admin password file on the VPS** — `ssh my-vps "sudo sh -c 'umask 077; openssl rand -base64 24 > /opt/keys/grafana-admin-password'"` (keeps the secret on the VPS only; owner reads it from there when logging in).
- [ ] **Step 2: Deploy** — `./scripts/deploy-hub.sh`. Expected: compose pulls, both containers `Up`.
- [ ] **Step 3: Verify from the Mac (over tailnet):**

```bash
source config/estate.env
curl -s "http://$AIOBS_HUB_TAILNET_IP:8428/health"          # expect: OK
curl -s -o /dev/null -w '%{http_code}\n' "http://$AIOBS_HUB_TAILNET_IP:3000/login"   # expect: 200
```
- [ ] **Step 4: Negative check (nothing public):** `curl -s -m 5 -o /dev/null -w '%{http_code}\n' "http://203.0.113.10:8428/health" || echo BLOCKED` — expect timeout/refused, **not** OK.
- [ ] **Step 5: Reboot resilience note** — `ssh my-vps "sudo systemctl is-enabled tailscaled docker"` → both `enabled`. Expected with the sysctl from Task 3: full stack returns after reboot. (Do not reboot the VPS to prove it — a-keeper-service keeper runs there; note as accepted residual.)

---

### Task 5: GPU box exporters (WSL2) — watts + server metrics flowing

**Files:**
- Create: `gpu-box/nvidia-gpu-exporter.service`, `gpu-box/vmagent.service`, `gpu-box/scrape.yml.tmpl`, `gpu-box/deploy-gpu-box.sh`

**Interfaces:**
- Consumes: hub VM endpoint from Task 4.
- Produces: series `nvidia_smi_power_draw_watts{host="win5090"}` etc. and the server's `sglang:*` (later `llamacpp:*`) series in VM, 15 s resolution.

- [ ] **Step 1: Write `gpu-box/scrape.yml.tmpl`**

```yaml
global: { scrape_interval: 15s }
scrape_configs:
  - job_name: gpu
    static_configs: [{ targets: ["127.0.0.1:__GPU_PORT__"], labels: { host: "__HOST_LABEL__" } }]
  - job_name: llm
    static_configs: [{ targets: ["__LLM_TARGET__"], labels: { host: "__HOST_LABEL__" } }]
```

- [ ] **Step 2: Write the two systemd units**

`gpu-box/nvidia-gpu-exporter.service`:
```ini
[Unit]
Description=nvidia_gpu_exporter (aiobs)
After=network.target
[Service]
ExecStart=/opt/aiobs/nvidia_gpu_exporter --web.listen-address=127.0.0.1:__GPU_PORT__
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
```
`gpu-box/vmagent.service`:
```ini
[Unit]
Description=vmagent (aiobs)
After=network-online.target
[Service]
ExecStart=/opt/aiobs/vmagent -promscrape.config=/opt/aiobs/scrape.yml \
  -remoteWrite.url=http://__HUB_IP__:__VM_PORT__/api/v1/write \
  -remoteWrite.tmpDataPath=/opt/aiobs/buffer -remoteWrite.maxDiskUsagePerURL=1GB
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Write `gpu-box/deploy-gpu-box.sh`** — sources `config/estate.env`; builds a WSL-side install script (download latest `nvidia_gpu_exporter` linux-amd64 release binary and `vmagent` from the matching `vmutils-linux-amd64` tarball into `/opt/aiobs/`, render templates by substituting `__GPU_PORT__`, `__HOST_LABEL__`, `__LLM_TARGET__`, `__HUB_IP__`, `__VM_PORT__`, install units, `systemctl daemon-reload && systemctl enable --now` both); ships it via the base64 recipe:

```bash
b64=$(base64 < /tmp/aiobs-box-install.sh)
ssh "$AIOBS_BOX_SSH_HOST" "wsl -d $AIOBS_BOX_WSL_DISTRO -e bash -lc \"echo $b64 | base64 -d | sudo bash\""
```
(If `AIOBS_BOX_WSL_DISTRO` is empty, run the script over plain ssh — that's the third-party Linux path.)

- [ ] **Step 4: Preflight the box** — confirm WSL systemd is active before installing: `ssh my-gpu-box "wsl -d Ubuntu-24.04 -e bash -lc 'systemctl is-system-running || true'"`. Accept `running`/`degraded`; if `offline`, stop — enable systemd in `/etc/wsl.conf` is an owner decision (flag and wait).
- [ ] **Step 5: Deploy** — run `gpu-box/deploy-gpu-box.sh`; then verify on-box: `…bash -lc 'curl -s localhost:9835/metrics | grep -m1 power_draw; systemctl is-active vmagent nvidia-gpu-exporter'`.
- [ ] **Step 6: Verify end-to-end from the Mac** —

```bash
source config/estate.env
curl -s "http://$AIOBS_HUB_TAILNET_IP:8428/api/v1/query?query=nvidia_smi_power_draw_watts" | jq -r '.data.result[0].value[1]'
curl -s "http://$AIOBS_HUB_TAILNET_IP:8428/api/v1/query" --data-urlencode 'query=count({job="llm"})' | jq -r '.data.result[0].value[1]'
```
Expected: a plausible wattage (idle ≈ 5–30 W) and a nonzero series count.
- [ ] **Step 7: Commit** — `git add gpu-box && git commit -m "feat(gpu-box): exporter+vmagent units, templated deploy"`

---

### Task 6: llama.cpp `--metrics` (approved change, console estate)

**Files:**
- Modify: llama.cpp lane `start-*.sh` **live on the box** under `/opt/llm/console/`, then sync the mirror in `~/Documents/New project/local-llm-console/remote/` (separate repo — live is truth; mirror is known to drift).

**Interfaces:**
- Produces: `llamacpp:*` metrics on `127.0.0.1:8002/metrics` whenever a llama.cpp lane is resident; vmagent (Task 5) already scrapes that target.

- [ ] **Step 1: Enumerate llama.cpp lanes live** — `ssh my-gpu-box "wsl -d Ubuntu-24.04 -e bash -lc 'grep -l llama-server /opt/llm/console/start-*.sh'"`. Record the exact list (expect the ornith and qwen-rvn family; dspark is SGLang — untouched).
- [ ] **Step 2: Confirm idle** — `curl -s http://127.0.0.1:18002/metrics | grep -m1 num_running` (SGLang) or check no active generation; **do not edit scripts mid-generation** (edits are safe while running, but avoids confusion during the later restart).
- [ ] **Step 3: Edit live scripts** — for each file from Step 1 that lacks it, insert `  --metrics \` into the `llama-server` argument block (backup first: `cp f f.bak-20260829`). Show diff via `bash -lc 'diff f.bak-20260829 f'`.
- [ ] **Step 4: Sync mirror** — copy the edited live scripts over their mirror copies in `local-llm-console/remote/`, `git diff` to confirm only `--metrics` lines changed, commit **in that repo**: `git commit -m "feat(lanes): enable llama.cpp Prometheus metrics (--metrics) on llama lanes"`.
- [ ] **Step 5: Verification is owner-gated** — takes effect at the next lane switch. Ask the owner to run their normal `vector-modelctl start ornith …` at a convenient moment (never initiated autonomously); then `curl -s http://127.0.0.1:18002/metrics | grep -m3 '^llamacpp'` and record the **actual metric names** for Task 7's queries. Until then, dashboards rely on `sglang:*`.

---

### Task 7: Estate dashboard rows 1–2 + GPU detail dashboard (Phase 1 acceptance)

**Files:**
- Create: `hub/grafana/dashboards/estate.json`, `hub/grafana/dashboards/gpu-detail.json`, `hub/grafana/dashboards/inference-detail.json`

**Interfaces:**
- Consumes: series from Tasks 5–6; datasource uid `vm` from Task 2.
- Produces: dashboard UIDs `aiobs-estate`, `aiobs-gpu`, `aiobs-inference` (stable — Task 12 edits `aiobs-estate` in place).

- [ ] **Step 1: Seed supporting dashboards** — download published JSONs and re-point the datasource:

```bash
curl -s https://grafana.com/api/dashboards/14574/revisions/latest/download -o hub/grafana/dashboards/gpu-detail.json
jq '(.. | .datasource? | select(. != null)) |= {"type":"prometheus","uid":"vm"} | .uid="aiobs-gpu" | .title="GPU Detail"' \
  hub/grafana/dashboards/gpu-detail.json > tmp && mv tmp hub/grafana/dashboards/gpu-detail.json
```
Same pattern for SGLang's official dashboard JSON (from the sglang repo, `examples/monitoring` path — locate with `firecrawl map` if it moved) → `inference-detail.json`, uid `aiobs-inference`. **Verify every query's metric name against live `/metrics` output** (known naming drift) — fix queries, don't trust the file.

- [ ] **Step 2: Write `estate.json` rows 1–2** — hand-written JSON, uid `aiobs-estate`, dark style, panels with these exact queries (datasource uid `vm` throughout):

| Panel | Type | Query |
|---|---|---|
| GPU power now | gauge (max 600) | `nvidia_smi_power_draw_watts` |
| Watts vs tok/s (24 h) | timeseries, 2 axes | A: `nvidia_smi_power_draw_watts` · B: `sum(rate(sglang:generation_tokens_total[1m])) or sum(rate(llamacpp_tokens_predicted_total[1m]))` (adjust B's llamacpp name to Task 6 Step 5 findings) |
| Energy today | stat | `avg_over_time(nvidia_smi_power_draw_watts[$__range]) * $__range_s / 3600 / 1000` (kWh) |
| VRAM / Temp | timeseries | `nvidia_smi_memory_used_bytes` · `nvidia_smi_temperature_gpu` |
| Prompt vs generation (by model) | timeseries stacked | `sum by (model_name)(rate(sglang:prompt_tokens_total[5m]))` + generation twin (+ llamacpp equivalents) |
| Latency | timeseries | `histogram_quantile(0.5, sum by (le)(rate(sglang:e2e_request_latency_seconds_bucket[5m])))` + p95 twin (verify exact histogram name against live output) |

- [ ] **Step 3: Redeploy + reload** — `./scripts/deploy-hub.sh` (provisioning picks JSON up on restart; `sudo docker compose restart grafana` if needed).
- [ ] **Step 4: Phase 1 acceptance (evidence, not assertion)** — fire one small generation through the existing tunnel:

```bash
curl -s http://127.0.0.1:18002/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen38-nvfp4","messages":[{"role":"user","content":"Reply with one word."}],"max_tokens":16}'
```
Then query VM for the last 5 min of `nvidia_smi_power_draw_watts` (expect a spike above idle) and `increase(sglang:generation_tokens_total[5m])` (expect > 0). Screenshot the dashboard over tailnet in the Browser pane for the owner.
- [ ] **Step 5: Disk headroom check** — `ssh my-vps "df -h / | tail -1"`; expect ≥9 GB free still. Record.
- [ ] **Step 6: Commit** — `git add hub/grafana/dashboards && git commit -m "feat(dashboards): estate rows 1-2 + seeded GPU/inference detail"`

---

### Task 8: Collector core (TDD — schema, exposition, state, lane harness)

**Files:**
- Create: `workstation/aiobs_collector/__init__.py`, `workstation/aiobs_collector/core.py`, `workstation/tests/test_core.py`

**Interfaces:**
- Produces (consumed by Tasks 9–11):

```python
@dataclass(frozen=True)
class Sample:            # one timestamped point
    metric: str          # e.g. "aiobs_tokens_total"
    labels: dict[str, str]
    value: float
    ts_ms: int

def render_exposition(samples: list[Sample]) -> str    # "metric{k=\"v\"} value ts_ms" lines
def load_config(path: str) -> dict[str, str]           # parses estate.env (KEY=VALUE, ~ expanded)
def load_state(state_dir: str) -> dict; def save_state(state_dir: str, state: dict)
class Lane(Protocol):  name: str;  def collect(self, cfg, state) -> list[Sample]
def run_lanes(lanes, cfg, state, now_ms) -> tuple[list[Sample], dict]
    # per-lane try/except; appends aiobs_lane_up{lane=} 1/0 and
    # aiobs_lane_last_success_timestamp{lane=} (kept from state on failure)
```

- [ ] **Step 1: Write failing tests** in `workstation/tests/test_core.py` (unittest): exposition escaping + ordering (`aiobs_tokens_total{kind="input",model="opus",origin="client",provider="claude-code"} 123 1756400000000` — labels sorted, quotes escaped); `load_config` expands `~` and ignores comments/blank lines; `run_lanes` isolates a raising lane (good lane's samples survive; `aiobs_lane_up{lane="bad"}` = 0, `=1` for good; last_success for bad lane carried from prior state).
- [ ] **Step 2: Run to fail** — `cd workstation && /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m unittest discover -s tests -v`. Expected: ImportError/failures.
- [ ] **Step 3: Implement `core.py`** minimally (stdlib only: dataclasses, json, os, time, urllib absent here — push lives in Task 11).
- [ ] **Step 4: Run to pass** — same command, all green.
- [ ] **Step 5: Commit** — `git add workstation && git commit -m "feat(collector): core schema, exposition renderer, env config, lane harness (TDD)"`

---

### Task 9: tokscale lane

**Files:**
- Create: `workstation/aiobs_collector/lane_tokscale.py`, `workstation/tests/test_lane_tokscale.py`, `workstation/tests/fixtures/tokscale_sample.json`

**Interfaces:**
- Consumes: `Sample`, `Lane` from Task 8.
- Produces: `TokscaleLane(name="tokscale")` emitting `aiobs_tokens_total`/`aiobs_cost_usd_total` with `origin="client"`, day-granular timestamps (ts = end of that day, or `now` for today), providers mapped: `claude-code`, `codex`, `cursor`, `droid`, `hermes` (tokscale client names → these canonical labels).

- [ ] **Step 1: Pin + capture real output** — `npm view tokscale version` → write `AIOBS_TOKSCALE_VERSION` into both env files; run `npx -y tokscale@$VER --json > /tmp/tokscale-raw.json` and `npx -y tokscale@$VER graph --output /tmp/tokscale-graph.json`; inspect the actual shape (`jq 'keys' + one sliced record`). **Redact** a two-day, two-client slice into `tests/fixtures/tokscale_sample.json` (token counts are fine; no paths/project names).
- [ ] **Step 2: Write failing tests against the fixture** — asserts: per provider+model+kind cumulative counters; cost series present when fixture has cost; unknown clients pass through with sanitized provider label (lowercase, non-alphanumeric → `-`); Factory absent-from-fixture ⇒ no droid series and **no exception** (spec: empty lane reports zero, never fails).
- [ ] **Step 3: Run to fail**, **Step 4: implement** (`subprocess.run(["npx","-y",f"tokscale@{ver}","--json"], …)` with 120 s timeout; pure normalize function `normalize_tokscale(doc: dict, now_ms: int) -> list[Sample]` tested directly on the fixture), **Step 5: run to pass**.
- [ ] **Step 6: Live smoke** — run the lane once for real; print sample count per provider; expect claude-code + codex + hermes > 0 (cursor/droid may be 0 — fine; note which).
- [ ] **Step 7: Commit** — `git commit -am "feat(collector): tokscale lane with fixture-driven normalization"`

---

### Task 10: OpenRouter lane

**Files:**
- Create: `workstation/aiobs_collector/lane_openrouter.py`, `workstation/tests/test_lane_openrouter.py`, `workstation/tests/fixtures/openrouter_activity.json`

**Interfaces:**
- Consumes: `Sample`, `Lane` from Task 8; cfg keys `AIOBS_OPENROUTER_ENV_FILE`, `AIOBS_OPENROUTER_KEY_NAME`.
- Produces: `OpenRouterLane(name="openrouter")` emitting `aiobs_tokens_total{provider="openrouter",model=…,kind=input|output,origin="client"}` and `aiobs_cost_usd_total{provider="openrouter",…}` per day.

- [ ] **Step 1: Verify key var name (name only, never the value)** — `grep -o 'OPENROUTER[A-Z_]*' ~/.hermes/.env | sort -u`; set `AIOBS_OPENROUTER_KEY_NAME` accordingly in `config/estate.env`.
- [ ] **Step 2: Capture one real response** — `GET https://openrouter.ai/api/v1/activity` with the bearer key (via a throwaway shell command that reads the key from the env file without echoing it); redact into `fixtures/openrouter_activity.json` (keep 2–3 daily rows).
- [ ] **Step 3: Write failing tests** — normalization from fixture: one input + one output tokens sample and one cost sample per (day, model); timestamps at day end; missing key file ⇒ `LaneConfigError` (caught by harness ⇒ `aiobs_lane_up 0`, run continues).
- [ ] **Step 4–5: implement (urllib.request, 30 s timeout) → green.**
- [ ] **Step 6: Commit** — `git commit -am "feat(collector): openrouter lane (official activity API)"`

---

### Task 11: Push, backfill, launchd — collector goes live (Phase 2 acceptance)

**Files:**
- Create: `workstation/aiobs_collector/__main__.py` (arg parsing: `--config`, `--backfill`, `--dry-run`), `workstation/aiobs_collector/push.py`, `workstation/tests/test_push.py`, `workstation/com.aiobs.collector.plist.tmpl`, `workstation/install-collector.sh`

**Interfaces:**
- Consumes: everything above.
- Produces: `push_samples(vm_base_url, samples)` POSTing exposition to `/api/v1/import/prometheus` (idempotent); launchd job `com.aiobs.collector` every 600 s logging to `~/Library/Logs/aiobs/collector.log`.

- [ ] **Step 1: TDD `push.py`** — test with a stdlib `http.server` stub: correct path, body matches `render_exposition`, non-2xx raises. Implement with urllib. Green.
- [ ] **Step 2: `__main__.py`** — load config/state → build enabled lanes from `AIOBS_LANES` (unknown name = hard error, per contract) → `run_lanes` → dedupe against state (skip days already pushed with identical values; always re-push today) → push (or print on `--dry-run`) → save state.
- [ ] **Step 3: Dry-run** — `python3 -m aiobs_collector --config ../config/estate.env --dry-run | head`; eyeball series.
- [ ] **Step 4: Backfill** — `--backfill` pushes *all* history the lanes returned (tokscale daily export = months of Claude/Codex). Run it once. Verify depth from VM: `query_range` over the past 60 days of `sum by (provider)(increase(aiobs_tokens_total[1d]))` returns rows for claude-code and codex well before today.
- [ ] **Step 5: launchd** — render plist from tmpl (`__PYTHON__`, `__REPO__`, `__CONFIG__` substitutions; `StartInterval` 600, `StandardErrorPath` the log), `launchctl bootstrap gui/$(id -u) …`, then `launchctl print gui/$(id -u)/com.aiobs.collector | grep state`. Two cycles later, `aiobs_lane_last_success_timestamp` in VM advances.
- [ ] **Step 6: Phase 2 acceptance** — same-day totals: `tokscale` TUI daily figure vs dashboard's `sum(increase(aiobs_tokens_total{provider="claude-code"}[1d]))` (± rounding); OpenRouter day vs its own dashboard for one sample day. Record both comparisons.
- [ ] **Step 7: Commit** — `git add workstation && git commit -m "feat(collector): push+backfill+launchd — cloud lanes live"`

---

### Task 12: Estate dashboard rows 3–4, README quickstart, wrap

**Files:**
- Modify: `hub/grafana/dashboards/estate.json`, `README.md`

**Interfaces:** consumes `aiobs_*` series (Tasks 9–11) and lane-health metrics.

- [ ] **Step 1: Add rows 3–4 to `estate.json`:**

| Panel | Type | Query |
|---|---|---|
| Daily tokens by provider | bars stacked | `sum by (provider)(increase(aiobs_tokens_total{origin="client",provider!="hermes"}[1d]))` |
| Cost per day | bars | `sum by (provider)(increase(aiobs_cost_usd_total[1d]))` |
| Month-to-date | stat | same increase over `[$__range]` with MTD range default |
| Model breakdown | table/pie | `topk(12, sum by (provider,model)(increase(aiobs_tokens_total[30d])))` |
| Local vs cloud share | pie | local: `sum(increase(sglang:generation_tokens_total[1d])) + sum(increase(llamacpp_tokens_predicted_total[1d]))` (Task 6 names) vs cloud: the provider sum above — **origins never mixed in one series** (hermes excluded from cloud sum: its tokens are already in the server-side local count and the openrouter lane) |
| Hermes per-profile attribution | timeseries | `sum by (model)(increase(aiobs_tokens_total{provider="hermes"}[1d]))` — labeled "client-side view" |
| Collector health | stat/table | `aiobs_lane_up`, `time() - aiobs_lane_last_success_timestamp` |

- [ ] **Step 2: Redeploy, screenshot all four rows over tailnet** for the owner.
- [ ] **Step 3: Finalize README** — full third-party quickstart: prerequisites (Docker host + Tailscale account, NVIDIA box, Node for tokscale), the three deploy rituals (`deploy-hub.sh`, `deploy-gpu-box.sh`, `install-collector.sh`), config reference table for every `AIOBS_*` var, privacy posture, troubleshooting (WSL systemd off, tailnet IP unset, lane_up=0 meanings), MIT. A stranger must be able to follow it without this repo's docs/ folder.
- [ ] **Step 4: Fresh-clone rehearsal (reusability acceptance)** — `git clone . /tmp/aiobs-rehearsal && cd /tmp/aiobs-rehearsal && cp config/estate.example.env config/estate.env && bash -n scripts/deploy-hub.sh gpu-box/deploy-gpu-box.sh workstation/install-collector.sh && python3 -m unittest discover -s workstation/tests` — everything runs from a clean copy with only the example env; no reference to owner paths escapes into tracked files (`git grep -iE 'remy|suhail|100\.[0-9]+\.' -- ':!docs'` returns nothing).
- [ ] **Step 5: Commit** — `git commit -am "feat: estate dashboard complete + third-party README"`. Phase 3 (alerting, Sankey, CodexBar limits, screenshots for publishing) remains a separate owner decision.
