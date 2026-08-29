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

## Quickstart

### Hub
Full quickstart lands with the final task; see docs/superpowers/plans/ meanwhile.

### GPU Box
Full quickstart lands with the final task; see docs/superpowers/plans/ meanwhile.

### Workstation
Full quickstart lands with the final task; see docs/superpowers/plans/ meanwhile.

## Privacy

This system captures token counts, timestamps, and cost only—never prompt or response content.

## License

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
