#!/usr/bin/env bash
# Installs/reinstalls the workstation collector as a launchd user agent
# (com.aiobs.collector), running `python3 -m aiobs_collector` every 600s.
#
# Template rendering happens HERE, on the operator's machine: __PLACEHOLDER__
# values in com.aiobs.collector.plist.tmpl are resolved from the local
# environment (python3/npx locations via `command -v`, this repo's own
# absolute path, config/estate.env's absolute path, the real $HOME-based log
# path) and substituted before the plist is ever written to LaunchAgents --
# same pattern gpu-box/deploy-gpu-box.sh uses for its own two templates.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"
source config/estate.env

for v in AIOBS_LANES AIOBS_STATE_DIR AIOBS_HUB_TAILNET_IP AIOBS_VM_PORT; do
  [ -n "${!v:-}" ] || { echo "FATAL: $v is unset/empty in config/estate.env" >&2; exit 1; }
done

PYTHON_BIN="$(command -v python3 || true)"
[ -n "$PYTHON_BIN" ] || { echo "FATAL: no python3 on PATH -- install Python 3 first" >&2; exit 1; }

NPX_BIN="$(command -v npx || true)"
[ -n "$NPX_BIN" ] || { echo "FATAL: no npx on PATH -- install Node.js first (the tokscale lane needs it)" >&2; exit 1; }
NPX_DIR="$(dirname "$NPX_BIN")"

CONFIG_PATH="$REPO_DIR/config/estate.env"
LOG_DIR="$HOME/Library/Logs/aiobs"
LOG_FILE="$LOG_DIR/collector.log"
mkdir -p "$LOG_DIR"

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS_DIR"
PLIST_DEST="$LAUNCH_AGENTS_DIR/com.aiobs.collector.plist"

echo "python3:  $PYTHON_BIN"
echo "npx dir:  $NPX_DIR"
echo "repo:     $REPO_DIR"
echo "config:   $CONFIG_PATH"
echo "log file: $LOG_FILE"

sed -e "s#__PYTHON__#${PYTHON_BIN}#g" \
    -e "s#__REPO__#${REPO_DIR}#g" \
    -e "s#__CONFIG__#${CONFIG_PATH}#g" \
    -e "s#__NPX_DIR__#${NPX_DIR}#g" \
    -e "s#__LOG_FILE__#${LOG_FILE}#g" \
    workstation/com.aiobs.collector.plist.tmpl > "$PLIST_DEST"

# Fail loudly rather than hand launchd a plist with an unresolved
# __PLACEHOLDER__ still in it (same guard gpu-box/deploy-gpu-box.sh uses).
if grep -l '__[A-Z_]*__' "$PLIST_DEST" 2>/dev/null; then
  echo "FATAL: unresolved __PLACEHOLDER__ in $PLIST_DEST above -- aborting" >&2
  exit 1
fi

plutil -lint "$PLIST_DEST"

# Unload any prior copy of this job first (a fresh bootstrap over a
# still-loaded one errors) -- harmless no-op on a first-ever install.
launchctl bootout "gui/$(id -u)/com.aiobs.collector" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"

echo "== com.aiobs.collector installed =="
launchctl print "gui/$(id -u)/com.aiobs.collector" | grep -E 'state|last exit'
