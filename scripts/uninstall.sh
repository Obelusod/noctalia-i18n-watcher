#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/noctalia-i18n-watcher
CONFIG_DIR=/etc/noctalia-i18n-watcher
STATE_DIR=/var/lib/noctalia-i18n-watcher
SYSTEMD_DIR=/etc/systemd/system
MANAGER_PATH=/usr/local/sbin/noctalia-i18n-watcher
SERVICE_USER=noctalia-i18n-watcher
SERVICE_NAME=noctalia-i18n-watcher.service
TIMER_NAME=noctalia-i18n-watcher.timer

if [[ $EUID -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

purge=false
case ${1:-} in
  "") ;;
  --purge) purge=true ;;
  *)
    echo "Usage: uninstall.sh [--purge]" >&2
    exit 2
    ;;
esac

exec 9>/run/lock/noctalia-i18n-watcher.lock
if ! flock -n 9; then
  echo "Another Noctalia i18n Watcher operation is in progress." >&2
  exit 1
fi

if [[ -e "$SYSTEMD_DIR/$TIMER_NAME" ]]; then
  systemctl disable --now "$TIMER_NAME"
fi
if [[ -e "$SYSTEMD_DIR/$SERVICE_NAME" ]]; then
  systemctl stop "$SERVICE_NAME"
fi

if [[ $purge == true && -e $SYSTEMD_DIR/$SERVICE_NAME ]]; then
  systemctl clean --what=state "$SERVICE_NAME"
fi

rm -rf -- \
  "$SYSTEMD_DIR/$SERVICE_NAME" \
  "$SYSTEMD_DIR/$SERVICE_NAME.d" \
  "$SYSTEMD_DIR/$TIMER_NAME" \
  "$SYSTEMD_DIR/$TIMER_NAME.d" \
  "$APP_DIR"
rm -f -- "$MANAGER_PATH"
systemctl daemon-reload

if [[ $purge == true ]]; then
  rm -rf -- "$STATE_DIR"
  rm -rf -- "$CONFIG_DIR"
  if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    userdel "$SERVICE_USER"
  fi
  if getent group "$SERVICE_USER" >/dev/null; then
    groupdel "$SERVICE_USER"
  fi
  echo "Uninstallation complete; configuration and state were removed."
else
  echo "Uninstallation complete; configuration and state were preserved."
fi
