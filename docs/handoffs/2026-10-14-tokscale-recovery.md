# Collector timeout recovery — 2026-10-14

Owner authorized all three repairs and live verification; leave changes uncommitted.

## Actions and current state
- Revalidated command identity before SIGTERM to the home-wide search PID 6620 and orphaned tokscale wrapper/native PIDs 52996/53309. All exited; old blocked collector PID 41737 subsequently completed. No unrelated process was stopped.
- `lane_tokscale.py` now delegates to `tokscale_runtime.py`: resolve the exact configured version from an installed native npm-cache package; no npm/Node wrapper or registry lookup per collection. Missing pins fail explicitly. Darwin and Linux GNU/musl package naming are handled.
- The native subprocess gets its own session/process group. On timeout (300 seconds), send SIGKILL to that group; cleanup has a separate five-second bound. Kernel uninterruptible I/O can delay physical exit despite the signal.
- Ran the existing launchd job once with `launchctl kickstart` after full-suite success. No schedule/plist or hub deployment changes. The job uses this checkout directly, so uncommitted source is active on future runs.
- Fresh collector receipt: HEALTHY, no failed lanes, run timestamp 2026-10-14T04:39:41.422Z, 284 samples pushed. All five lanes have that same fresh success timestamp in VictoriaMetrics. GPU exporter is up. The actual live Grafana panel 107 expression returned 0 (All up).

## Verification and evidence
- Full workstation suite: 347 tests passed; `git diff --check` clean.
- Real subprocess regression covers timeout termination of a descendant ignoring SIGTERM, exit error propagation, and stdout. Temporary installed-package fixtures cover offline exact-version selection/rejection and Linux libc naming.
- Private evidence: `private/recovery-20261014/verification.json`, `live-pipeline-panel.json`, `tests.log` (gitignored). This was one full live recovery cycle; later scheduled cycles were not yet observed.

## Change custody and next step
- New runtime and regression test; updated lane, relevant test mocks, README setup/troubleshooting, and dated owner decision in AGENTS.md.
- Preserved pre-existing edits in `claude_usage.py`, `lane_usage.py`, `prices.py`, `test_claude_usage.py`, and the existing Haiku test in `test_lane_usage.py`. Only mock targets were changed in that last file by this repair.
- Nothing staged, committed, pushed, or deployed to the hub. No further owner decision is needed for this completed repair. Commit/push remain separate owner gates.
- If the npm cache is cleared, reinstall the configured pin once with `npx -y tokscale@<version> --version` using the collector's user/cache. Do not change the pin or backfill history just to resolve a missing binary.
