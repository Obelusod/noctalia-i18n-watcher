#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
CONFIG_DIR=/etc/noctalia-i18n-watcher
MESSAGE_DIR=$CONFIG_DIR/messages
STATE_DIR=/var/lib/noctalia-i18n-watcher
SERVICE_USER=noctalia-i18n-watcher

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

if [[ -L $STATE_DIR ]]; then
  echo "State directory must not be a symbolic link: $STATE_DIR" >&2
  exit 1
fi

if ! getent group "$SERVICE_USER" >/dev/null; then
  groupadd --system "$SERVICE_USER"
fi
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SERVICE_USER" --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# Config remains private; message files contain no credentials.
install -d -o root -g root -m 0711 "$CONFIG_DIR"
install -d -o root -g root -m 0755 "$MESSAGE_DIR"
if [[ ! -e $STATE_DIR ]]; then
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$STATE_DIR"
fi

if [[ ! -e "$CONFIG_DIR/config.json" ]]; then
  install -o root -g "$SERVICE_USER" -m 0640 \
    "$SOURCE_DIR/config/config.json" "$CONFIG_DIR/config.json"
else
  chown root:"$SERVICE_USER" "$CONFIG_DIR/config.json"
  chmod 0640 "$CONFIG_DIR/config.json"
fi

while IFS= read -r message; do
  relative=${message#"$SOURCE_DIR/config/messages/"}
  target=$MESSAGE_DIR/$relative
  if [[ ! -e $target ]]; then
    install -d -o root -g root -m 0755 "$(dirname -- "$target")"
    install -o root -g root -m 0644 "$message" "$target"
  fi
done < <(
  find "$SOURCE_DIR/config/messages" -type f -name '*.yaml' -print | sort
)

"$SOURCE_DIR/scripts/update.sh"

echo "Installation complete. Edit $CONFIG_DIR/config.json, then run:"
echo "  sudo noctalia-i18n-watcher validate"
echo "  sudo systemctl enable --now noctalia-i18n-watcher.timer"
