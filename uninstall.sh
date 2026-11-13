#!/usr/bin/env bash
# Uninstall the MeshCore <-> Bitchat bridge.
#
# Undoes what install.sh does:
#   * stops/disables/removes the systemd service (if installed)
#   * removes the .venv/ virtual environment
#   * with --purge: also removes the state dir (identity keys + config + logs)
#
# Usage:
#   ./uninstall.sh             # remove systemd unit + venv, keep state dir
#   ./uninstall.sh --purge     # also remove ~/.config/meshcore-bitchat-bridge
#
# Not touched (manual steps from the README, undo by hand if desired):
#   * this git checkout itself (delete the folder when done)
#   * bluetooth group membership, /etc/bluetooth/main.conf tweaks,
#     the bluetoothd -P battery override
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
SYSTEMD_UNIT="meshcore-bitchat-bridge.service"
SYSTEMD_TARGET="/etc/systemd/system/${SYSTEMD_UNIT}"
STATE_DIR="${HOME}/.config/meshcore-bitchat-bridge"

PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

# ------------------------------------------------------------ systemd unit
if [[ -e "${SYSTEMD_TARGET}" && ${EUID} -ne 0 ]]; then
  echo "==> Removing the systemd unit needs root; re-running with sudo"
  exec sudo "$0" "$@"
fi

# when re-run under sudo, target the invoking user's state dir, not /root
if [[ ${EUID} -eq 0 && -n "${SUDO_USER:-}" ]]; then
  USER_HOME="$(getent passwd "${SUDO_USER}" | cut -d: -f6)"
  STATE_DIR="${USER_HOME}/.config/meshcore-bitchat-bridge"
fi

if [[ -e "${SYSTEMD_TARGET}" ]]; then
  echo "==> Removing ${SYSTEMD_UNIT}"
  systemctl stop "${SYSTEMD_UNIT}" 2>/dev/null || true
  systemctl disable "${SYSTEMD_UNIT}" 2>/dev/null || true
  rm -f "${SYSTEMD_TARGET}"
  # drop a stale enable symlink if disable could not remove it
  rm -f "/etc/systemd/system/multi-user.target.wants/${SYSTEMD_UNIT}"
  systemctl daemon-reload
  systemctl reset-failed "${SYSTEMD_UNIT}" 2>/dev/null || true
else
  echo "==> No systemd unit installed (${SYSTEMD_TARGET})"
fi

# ---------------------------------------------------------- virtualenv
if [[ -d "${VENV_DIR}" ]]; then
  echo "==> Removing virtual environment ${VENV_DIR}"
  if ! rm -rf "${VENV_DIR}" 2>/dev/null; then
    # legacy installs (install.sh before the --unit-only fix) created the
    # venv as root, so its files may need elevated removal
    echo "    (files owned by root; retrying with sudo)"
    sudo rm -rf "${VENV_DIR}"
  fi
else
  echo "==> No virtual environment found (${VENV_DIR})"
fi

# ------------------------------------------------------------- state dir
if [[ ${PURGE} -eq 1 ]]; then
  if [[ -d "${STATE_DIR}" ]]; then
    echo "==> Removing state dir ${STATE_DIR} (identity keys + config + logs)"
    rm -rf "${STATE_DIR}"
  else
    echo "==> No state dir found (${STATE_DIR})"
  fi
else
  echo "==> Keeping state dir ${STATE_DIR}"
  echo "    (contains the bridge identity; phones will recognise the bridge"
  echo "     again after a reinstall. Use --purge to delete it too.)"
fi

cat <<'EOF'

==> Uninstall complete.

Manual steps intentionally left alone:
  * bluetooth group membership (sudo gpasswd -d $USER bluetooth)
  * /etc/bluetooth/main.conf tweaks and the bluetoothd -P battery override
  * this checkout itself — delete the folder when you're done.
EOF
