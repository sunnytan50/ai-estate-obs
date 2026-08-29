#!/usr/bin/env bash
# Deploys nvidia_gpu_exporter + vmagent onto the GPU box (WSL2 or plain Linux).
#
# Template rendering happens HERE, on the operator's machine: __PLACEHOLDER__ values are
# substituted from config/estate.env into gpu-box/scrape.yml.tmpl and the two .service files
# before anything crosses the network. Only the rendered, concrete file contents and two
# pinned upstream release binaries ever reach the box. Never touches the box's LLM server
# (loopback :8002) — this only adds two new systemd units in WSL and downloads two binaries.
set -euo pipefail
cd "$(dirname "$0")/.."
source config/estate.env

for v in AIOBS_BOX_SSH_HOST AIOBS_BOX_HOST_LABEL AIOBS_LLM_METRICS_TARGET \
         AIOBS_GPU_EXPORTER_PORT AIOBS_HUB_TAILNET_IP AIOBS_VM_PORT AIOBS_VM_VERSION; do
  [ -n "${!v:-}" ] || { echo "FATAL: $v is unset/empty in config/estate.env" >&2; exit 1; }
done

# ---- Pinned upstream release tags ----
# nvidia_gpu_exporter has no existing pin slot in estate.env (Task 5 scope only touches
# gpu-box/*), so it is pinned here. Resolved via the GitHub releases API at implementation
# time -- see task-5-report.md for the verification. Bump deliberately, not silently.
NVIDIA_GPU_EXPORTER_TAG="v1.14.0"
# vmagent ships in the same release/tag as the VictoriaMetrics server binary, so it reuses
# the hub's existing AIOBS_VM_VERSION pin (also independently confirmed as GitHub's current
# latest release for VictoriaMetrics/VictoriaMetrics at implementation time) instead of a
# second, potentially-drifting pin of its own.
VMUTILS_TAG="${AIOBS_VM_VERSION}"

# NOTE: the non-NVML asset is intentional -- it shells out to `nvidia-smi` (confirmed present
# and working in this box's WSL distro), which is the build that reports the
# `nvidia_smi_power_draw_watts` metric this task's Interfaces line names. The NVML-linked
# asset (`nvidia_gpu_exporter-nvml_*`) uses different metric names and needs libnvidia-ml
# linkage that WSL2 does not reliably expose the same way.
NVIDIA_GPU_EXPORTER_URL="https://github.com/utkuozdemir/nvidia_gpu_exporter/releases/download/${NVIDIA_GPU_EXPORTER_TAG}/nvidia_gpu_exporter_${NVIDIA_GPU_EXPORTER_TAG#v}_linux_x86_64.tar.gz"
VMUTILS_URL="https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/${VMUTILS_TAG}/vmutils-linux-amd64-${VMUTILS_TAG}.tar.gz"

render() {
  sed -e "s#__GPU_PORT__#${AIOBS_GPU_EXPORTER_PORT}#g" \
      -e "s#__HOST_LABEL__#${AIOBS_BOX_HOST_LABEL}#g" \
      -e "s#__LLM_TARGET__#${AIOBS_LLM_METRICS_TARGET}#g" \
      -e "s#__HUB_IP__#${AIOBS_HUB_TAILNET_IP}#g" \
      -e "s#__VM_PORT__#${AIOBS_VM_PORT}#g" \
      "$1"
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

render gpu-box/scrape.yml.tmpl             > "$WORKDIR/scrape.yml"
render gpu-box/nvidia-gpu-exporter.service > "$WORKDIR/nvidia-gpu-exporter.service"
render gpu-box/vmagent.service             > "$WORKDIR/vmagent.service"

# Fail loudly rather than ship a unit/config with an unresolved __PLACEHOLDER__ left in it.
if grep -l '__[A-Z_]*__' "$WORKDIR"/scrape.yml "$WORKDIR"/*.service 2>/dev/null; then
  echo "FATAL: unresolved __PLACEHOLDER__ in a rendered file above -- aborting" >&2
  exit 1
fi

INSTALLER=/tmp/aiobs-box-install.sh
{
  # Unquoted heredoc: these two URLs are resolved HERE (Mac side) and baked in as literal
  # strings; \$(date ...) is escaped so it evaluates on the box, at install time, instead.
  cat <<INSTALLER_HEAD
#!/usr/bin/env bash
set -euo pipefail
echo "== aiobs gpu-box install: \$(date -u +%FT%TZ) =="

NVIDIA_GPU_EXPORTER_URL="${NVIDIA_GPU_EXPORTER_URL}"
VMUTILS_URL="${VMUTILS_URL}"
INSTALLER_HEAD

  # Quoted heredoc from here on: everything below runs verbatim on the box.
  cat <<'INSTALLER_BODY'
# --- assert nvidia-smi exists before installing or enabling anything ---
NVIDIA_SMI_BIN=""
for candidate in /usr/lib/wsl/lib/nvidia-smi /usr/bin/nvidia-smi /usr/local/bin/nvidia-smi; do
  if [ -x "$candidate" ]; then NVIDIA_SMI_BIN="$candidate"; break; fi
done
if [ -z "$NVIDIA_SMI_BIN" ]; then
  NVIDIA_SMI_BIN="$(command -v nvidia-smi || true)"
fi
if [ -z "$NVIDIA_SMI_BIN" ] || [ ! -x "$NVIDIA_SMI_BIN" ]; then
  echo "FATAL: nvidia-smi not found on this box -- aborting before installing/enabling anything" >&2
  exit 1
fi
echo "nvidia-smi OK: $NVIDIA_SMI_BIN"

# WSL2 keeps nvidia-smi under /usr/lib/wsl/lib, which is on an interactive login PATH but
# NOT on systemd's default unit PATH or sudo's secure_path (both verified as
# /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin[:...]  on this box) -- a bare `nvidia-smi`
# lookup from the exporter process would fail. Symlink it onto /usr/local/bin, which is on
# both, so nvidia-gpu-exporter.service's ExecStart (shipped unmodified) can find it. Harmless
# no-op on a plain Linux box where nvidia-smi is already on PATH.
if [ ! -e /usr/local/bin/nvidia-smi ]; then
  ln -s "$NVIDIA_SMI_BIN" /usr/local/bin/nvidia-smi
  echo "symlinked $NVIDIA_SMI_BIN -> /usr/local/bin/nvidia-smi (systemd/sudo default PATH)"
fi

mkdir -p /opt/aiobs/buffer
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "downloading nvidia_gpu_exporter from $NVIDIA_GPU_EXPORTER_URL"
curl -fsSL -o "$TMP/gpu-exp.tar.gz" "$NVIDIA_GPU_EXPORTER_URL"
mkdir -p "$TMP/gpu-exp"
tar xzf "$TMP/gpu-exp.tar.gz" -C "$TMP/gpu-exp"
GPU_BIN="$(find "$TMP/gpu-exp" -maxdepth 2 -type f -iname 'nvidia_gpu_exporter*' \
  ! -name '*.tar.gz' ! -name '*.txt' ! -name '*.md' -print -quit)"
[ -n "$GPU_BIN" ] || { echo "FATAL: nvidia_gpu_exporter binary not found in release archive" >&2; exit 1; }
install -m 0755 "$GPU_BIN" /opt/aiobs/nvidia_gpu_exporter
echo "installed $(basename "$GPU_BIN") -> /opt/aiobs/nvidia_gpu_exporter"

echo "downloading vmagent (vmutils) from $VMUTILS_URL"
curl -fsSL -o "$TMP/vmutils.tar.gz" "$VMUTILS_URL"
mkdir -p "$TMP/vmutils"
tar xzf "$TMP/vmutils.tar.gz" -C "$TMP/vmutils"
VMAGENT_BIN="$(find "$TMP/vmutils" -maxdepth 2 -type f -iname 'vmagent*' -print -quit)"
[ -n "$VMAGENT_BIN" ] || { echo "FATAL: vmagent binary not found in vmutils archive" >&2; exit 1; }
install -m 0755 "$VMAGENT_BIN" /opt/aiobs/vmagent
echo "installed $(basename "$VMAGENT_BIN") -> /opt/aiobs/vmagent"
INSTALLER_BODY

  echo "cat > /opt/aiobs/scrape.yml <<'AIOBS_SCRAPE_EOF'"
  cat "$WORKDIR/scrape.yml"
  echo "AIOBS_SCRAPE_EOF"

  echo "cat > /etc/systemd/system/nvidia-gpu-exporter.service <<'AIOBS_UNIT1_EOF'"
  cat "$WORKDIR/nvidia-gpu-exporter.service"
  echo "AIOBS_UNIT1_EOF"

  echo "cat > /etc/systemd/system/vmagent.service <<'AIOBS_UNIT2_EOF'"
  cat "$WORKDIR/vmagent.service"
  echo "AIOBS_UNIT2_EOF"

  cat <<'INSTALLER_TAIL'
systemctl daemon-reload
systemctl enable --now nvidia-gpu-exporter vmagent
echo "== aiobs gpu-box install complete =="
INSTALLER_TAIL
} > "$INSTALLER"

echo "rendered installer -> $INSTALLER ($(wc -l < "$INSTALLER" | tr -d ' ') lines)"

b64="$(base64 < "$INSTALLER" | tr -d '\n')"

if [ -n "${AIOBS_BOX_WSL_DISTRO:-}" ]; then
  ssh "$AIOBS_BOX_SSH_HOST" "wsl -d $AIOBS_BOX_WSL_DISTRO -e bash -lc \"echo $b64 | base64 -d | sudo bash\""
else
  # third-party Linux path: no wsl.exe wrapper, run directly over plain ssh
  ssh "$AIOBS_BOX_SSH_HOST" "echo $b64 | base64 -d | sudo bash"
fi
