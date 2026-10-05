# AGENTS.md — ai-estate-obs

Rules for any agent (Claude, Droid, Codex) working in this repository. Global context: `~/.claude/CLAUDE.md`.

## Scope
- Dashboards: `hub/grafana/dashboards/*.json` (provisioned from the repo — no click-built panels).
- Collector: `workstation/aiobs_collector/`; hub stack: `hub/`; GPU box: `gpu-box/`.
- This repo is edited by more than one session. Re-read a file from disk immediately before editing it, and never overwrite changes you did not make.

## Safety
- Grafana (:3000) and VictoriaMetrics (:8428) stay bound to the tailnet only. Never expose them publicly.
- No secrets in commits or output. `config/estate.env` is gitignored; the Grafana admin password lives on the hub at the path in `AIOBS_GRAFANA_ADMIN_PASSWORD_FILE`.
- Do not change the tested cost/token query rules without a reason and a passing lint: no `increase()` on the pushed counters, forward-looking daily bars (`offset -1d`), estate period totals anchored to the picked range with millisecond-exact `@ (${__from} / 1000)` / `@ (${__to} / 1000)` (whole-second `${__to:date:seconds}` drops the 23:59:59.999 day-end sample), live cards pinned to `now()`. See `workstation/tests/test_dashboards.py` and the README.
- `estate.json` is generated: edit `scripts/build_estate_dashboard.py`, rerun it, and the lint checks the two match. Its local-GPU and pipeline panels are carried over by id, so edit those in the JSON.
- Dashboard dollars are API list-price value from the `usage` lane (`workstation/aiobs_collector/prices.py`). Never edit a price entry to correct the past; add a new entry with a later `since`. Frozen days are never re-pushed.

## Tests
- Run the full suite from `workstation/` before any deploy: `python3 -m unittest discover -s tests` (337 tests on 2026-10-05). It must pass.

## Deploy
- Dashboards only: `rsync -az hub/grafana/dashboards/ remy-bot:/opt/observability/grafana/dashboards/`. Grafana re-reads provisioned files within ~10 s; no restart.
- Preview first: a copy with uid `aiobs-estate-preview` goes to the hub's dashboard dir only (never the repo — the uid test expects exactly 3 files), gets screenshotted at 1920 and 1440 wide, then is deleted.
- Rollback: `git checkout` the file, then the same rsync.
- A full `scripts/deploy-hub.sh` run needs explicit owner approval.

## Git
- Commits and pushes each need the owner's explicit go. Commit by explicit pathspec.
