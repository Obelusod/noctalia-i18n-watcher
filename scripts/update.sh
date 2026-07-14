#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
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

for command in flock groupadd python3 runuser systemctl systemd-analyze useradd; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing required command: $command" >&2
    exit 1
  fi
done

if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  echo "Missing configuration: $CONFIG_DIR/config.json" >&2
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
  echo "Python 3.12 or later is required." >&2
  exit 1
fi

if [[ -L $STATE_DIR ]]; then
  echo "State directory must not be a symbolic link: $STATE_DIR" >&2
  exit 1
fi

exec 9>/run/lock/noctalia-i18n-watcher.lock
if ! flock -n 9; then
  echo "Another Noctalia i18n Watcher operation is in progress." >&2
  exit 1
fi

if ! getent group "$SERVICE_USER" >/dev/null; then
  groupadd --system "$SERVICE_USER"
fi
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SERVICE_USER" --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi

STAGING_DIR=$(mktemp -d /opt/.noctalia-i18n-watcher.XXXXXX)
ROLLBACK_DIR=$(mktemp -d /opt/.noctalia-i18n-watcher.rollback.XXXXXX)
timer_was_active=false
deployment_started=false

restore_file() {
  local backup=$1 target=$2
  if [[ -e $backup ]]; then
    install -o root -g root -m 0644 "$backup" "$target"
  else
    rm -f -- "$target"
  fi
}

cleanup() {
  local status=$1
  trap - EXIT

  if [[ $status -ne 0 && $deployment_started == true ]]; then
    systemctl stop "$TIMER_NAME" "$SERVICE_NAME" 2>/dev/null || true
    rm -rf -- "$APP_DIR"
    if [[ -d $ROLLBACK_DIR/app ]]; then
      mv -- "$ROLLBACK_DIR/app" "$APP_DIR"
    fi
    restore_file "$ROLLBACK_DIR/$SERVICE_NAME" "$SYSTEMD_DIR/$SERVICE_NAME"
    restore_file "$ROLLBACK_DIR/$TIMER_NAME" "$SYSTEMD_DIR/$TIMER_NAME"
    if [[ -e $ROLLBACK_DIR/manager ]]; then
      install -o root -g root -m 0755 "$ROLLBACK_DIR/manager" "$MANAGER_PATH"
    else
      rm -f -- "$MANAGER_PATH"
    fi
    systemctl daemon-reload || true
  fi

  if [[ -n $STAGING_DIR ]]; then
    rm -rf -- "$STAGING_DIR"
  fi
  rm -rf -- "$ROLLBACK_DIR"
  if [[ $status -ne 0 && $timer_was_active == true ]]; then
    systemctl start "$TIMER_NAME" || \
      echo "Failed to restore $TIMER_NAME after the update error." >&2
  fi
  exit "$status"
}
trap 'cleanup $?' EXIT

while IFS= read -r message; do
  relative=${message#"$SOURCE_DIR/config/messages/"}
  target=$CONFIG_DIR/messages/$relative
  if [[ ! -e $target ]]; then
    install -d -o root -g root -m 0755 "$(dirname -- "$target")"
    install -o root -g root -m 0644 "$message" "$target"
  fi
done < <(
  find "$SOURCE_DIR/config/messages" -type f -name '*.yaml' -print | sort
)

while IFS= read -r module; do
  install -D -o root -g root -m 0644 "$SOURCE_DIR/$module" "$STAGING_DIR/$module"
done < <(
  cd "$SOURCE_DIR"
  find noctalia_i18n_watcher -type f -name '*.py' -print | sort
)
install -o root -g root -m 0644 "$SOURCE_DIR/requirements.txt" "$STAGING_DIR/"

python3 -m venv "$STAGING_DIR/.venv"
"$STAGING_DIR/.venv/bin/pip" install --disable-pip-version-check -r \
  "$STAGING_DIR/requirements.txt"
chmod -R u=rwX,go=rX "$STAGING_DIR"
CREDENTIALS_DIRECTORY=$CONFIG_DIR STATE_DIRECTORY=$STATE_DIR \
  /usr/bin/env --chdir="$STAGING_DIR" \
  "$STAGING_DIR/.venv/bin/python" -m noctalia_i18n_watcher check

if systemctl is-active --quiet "$TIMER_NAME"; then
  timer_was_active=true
  systemctl stop "$TIMER_NAME"
fi
if [[ -e "$SYSTEMD_DIR/$SERVICE_NAME" ]]; then
  systemctl stop "$SERVICE_NAME"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$STATE_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"
chown root:"$SERVICE_USER" "$CONFIG_DIR/config.json"
chmod 0640 "$CONFIG_DIR/config.json"

for unit in "$SERVICE_NAME" "$TIMER_NAME"; do
  if [[ -e $SYSTEMD_DIR/$unit ]]; then
    cp -a -- "$SYSTEMD_DIR/$unit" "$ROLLBACK_DIR/$unit"
  fi
done
if [[ -e $MANAGER_PATH ]]; then
  cp -a -- "$MANAGER_PATH" "$ROLLBACK_DIR/manager"
fi

if [[ -d $APP_DIR ]]; then
  mv -- "$APP_DIR" "$ROLLBACK_DIR/app"
fi
deployment_started=true
mv -- "$STAGING_DIR" "$APP_DIR"
STAGING_DIR=
install -o root -g root -m 0644 \
  "$SOURCE_DIR/systemd/$SERVICE_NAME" "$SYSTEMD_DIR/"
install -o root -g root -m 0644 \
  "$SOURCE_DIR/systemd/$TIMER_NAME" "$SYSTEMD_DIR/"
install -o root -g root -m 0755 "$SOURCE_DIR/scripts/manage.sh" "$MANAGER_PATH"
systemctl daemon-reload
systemd-analyze verify "$SYSTEMD_DIR/$SERVICE_NAME" "$SYSTEMD_DIR/$TIMER_NAME"
runuser -u "$SERVICE_USER" -- /usr/bin/env --chdir="$APP_DIR" \
  "$APP_DIR/.venv/bin/python" -m noctalia_i18n_watcher check

if [[ $timer_was_active == true ]]; then
  systemctl start "$TIMER_NAME"
  timer_was_active=false
fi
echo "Update complete."
