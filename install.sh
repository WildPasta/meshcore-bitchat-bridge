#!/usr/bin/env bash
# Install the MeshCore <-> Bitchat bridge on a Raspberry Pi (or any Linux box).
#
# Usage:
#   ./install.sh              # create venv + install deps
#   ./install.sh --systemd    # also install+enable the systemd service
#   PORT=/dev/ttyACM0 ./install.sh --systemd
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
SYSTEMD_UNIT="meshcore-bitchat-bridge.service"
SYSTEMD_TARGET="/etc/systemd/system/${SYSTEMD_UNIT}"
PORT="${PORT:-}"

WANT_SYSTEMD=0
UNIT_ONLY=0  # internal: set when re-executed under sudo (unit install only)
for arg in "$@"; do
  case "$arg" in
    --systemd) WANT_SYSTEMD=1 ;;
    --unit-only) UNIT_ONLY=1 ;;  # internal: venv/deps already installed as user
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

if [[ ${UNIT_ONLY} -eq 0 ]]; then
  echo "==> Creating virtual environment in ${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"

  echo "==> Installing dependencies"
  "${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null
  "${VENV_DIR}/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt"

  echo "==> Running offline self-tests"
  "${VENV_DIR}/bin/pip" install -r "${SCRIPT_DIR}/requirements-dev.txt" >/dev/null
  if ! "${VENV_DIR}/bin/python" -m pytest "${SCRIPT_DIR}/tests" -q; then
    echo "!! Self-tests failed — aborting. Please report this." >&2
    exit 1
  fi
fi

if [[ ${WANT_SYSTEMD} -eq 1 ]]; then
  if [[ ${EUID} -ne 0 ]]; then
    echo "==> --systemd needs root; installing unit only (PORT=${PORT:-auto})"
    exec sudo PORT="${PORT}" "$0" --systemd --unit-only
  fi

  RUN_USER="${SUDO_USER:-$(id -un)}"
  UNIT_FILE="${SCRIPT_DIR}/systemd/${SYSTEMD_UNIT}"
  TMP_UNIT="$(mktemp)"

  sed \
    -e "s|__DIR__|${SCRIPT_DIR}|g" \
    -e "s|__USER__|${RUN_USER}|g" \
    -e "s|__PORT__|${PORT}|g" \
    "${UNIT_FILE}" > "${TMP_UNIT}"

  # strip the --port arg entirely when no port was chosen (auto-detect)
  if [[ -z "${PORT}" ]]; then
    sed -i 's| --port __PORT__||' "${TMP_UNIT}"
  fi

  install -m 644 "${TMP_UNIT}" "${SYSTEMD_TARGET}"
  rm -f "${TMP_UNIT}"

  systemctl daemon-reload
  systemctl enable "${SYSTEMD_UNIT}"
  echo "==> Installed and enabled ${SYSTEMD_UNIT} (user: ${RUN_USER})"
  echo "    Start it with:  sudo systemctl start ${SYSTEMD_UNIT}"
fi

cat <<'EOF'

==> Almost done! For reliable Bluetooth (especially with iOS devices):

  1) Add your user to the bluetooth group (needed by the systemd service):
       sudo usermod -aG bluetooth $USER   # then log out/in

  2) iOS pairing popups — edit /etc/bluetooth/main.conf, [General] section:
       JustWorksRepairing = never
       ControllerMode = le
       Privacy = device

  3) Disable the BlueZ battery plugin (triggers pairing requests).
     Edit the bluetooth service (path may vary):
       sudo systemctl edit bluetooth.service
     and add:
       [Service]
       ExecStart=
       ExecStart=/usr/libexec/bluetooth/bluetoothd -P battery
     then: sudo systemctl daemon-reload && sudo systemctl restart bluetooth

==> Run manually (recommended for first test):
     .venv/bin/python bridge.py --debug

See README.md for full documentation.
EOF
