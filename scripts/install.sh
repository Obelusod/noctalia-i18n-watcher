#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
CONFIG_DIR=/etc/noctalia-i18n-watcher

if [[ $EUID -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This script supports Debian and Ubuntu systems with apt-get." >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv ca-certificates util-linux

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  echo "Python 3.12 or later is required." >&2
  exit 1
fi

install -d -o root -g root -m 0711 "$CONFIG_DIR"
if [[ ! -e "$CONFIG_DIR/config.json" ]]; then
  install -o root -g root -m 0600 \
    "$SOURCE_DIR/config/config.json" "$CONFIG_DIR/config.json"
fi

"$SOURCE_DIR/scripts/update.sh"

echo "Installation complete. Edit $CONFIG_DIR/config.json, then run:"
echo "  sudo noctalia-i18n-watcher validate"
echo "  sudo systemctl enable --now noctalia-i18n-watcher.timer"
