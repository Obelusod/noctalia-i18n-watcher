#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/noctalia-i18n-watcher
SERVICE_USER=noctalia-i18n-watcher
SERVICE_NAME=noctalia-i18n-watcher.service
TIMER_NAME=noctalia-i18n-watcher.timer
PYTHON=$APP_DIR/.venv/bin/python

show_help() {
  printf '%s\n' \
    "Usage: noctalia-i18n-watcher COMMAND" \
    "" \
    "Commands:" \
    "  run [--monitor ID] [--dry-run]" \
    "                  Collect changes and immediately deliver queued notifications" \
    "  reset {baseline,state} [--monitor ID] [--notify]" \
    "                  Rebuild the baseline or clear all monitoring state" \
    "  check [--monitor ID]" \
    "                  Validate configuration, messages, and state" \
    "  status [--monitor ID] [--notify]" \
    "                  Show monitoring state; optionally notify Discord" \
    "  changes PAGE [--monitor ID]" \
    "                  Show one page of source changes" \
    "  help            Show this help"
}

case ${1:-} in
  help | -h | --help)
    show_help
    exit
    ;;
esac

if [[ $EUID -ne 0 ]]; then
  echo "Run this command with sudo." >&2
  exit 1
fi

if [[ ! -x $PYTHON ]]; then
  echo "Noctalia i18n Watcher is not installed." >&2
  exit 1
fi

exec 9>/run/lock/noctalia-i18n-watcher.lock
if ! flock -n 9; then
  echo "Another Noctalia i18n Watcher operation is in progress." >&2
  exit 1
fi

run_application() {
  runuser -u "$SERVICE_USER" -- /usr/bin/env --chdir="$APP_DIR" \
    "$PYTHON" -m noctalia_i18n_watcher "$@"
}

timer_was_active=false

stop_timer() {
  if systemctl is-active --quiet "$TIMER_NAME"; then
    timer_was_active=true
    systemctl stop "$TIMER_NAME"
  fi
  systemctl stop "$SERVICE_NAME"
}

restore_timer() {
  if [[ $timer_was_active == true ]]; then
    systemctl start "$TIMER_NAME"
    timer_was_active=false
  fi
}

maintenance() {
  stop_timer
  trap restore_timer EXIT
  "$@"
  restore_timer
  trap - EXIT
}

show_status() {
  local enabled active result
  enabled=$(systemctl is-enabled "$TIMER_NAME" 2>/dev/null || true)
  active=$(systemctl is-active "$TIMER_NAME" 2>/dev/null || true)
  result=$(systemctl show "$SERVICE_NAME" --property=Result --value 2>/dev/null || true)
  echo "Timer: ${enabled:-unknown}, ${active:-unknown}"
  echo "Last service result: ${result:-unknown}"
  run_application status "$@"
}

case ${1:-} in
  check)
    run_application "$@"
    ;;
  changes)
    run_application "$@"
    ;;
  reset)
    maintenance run_application "$@"
    ;;
  run)
    dry_run=false
    for argument in "${@:2}"; do
      if [[ $argument == --dry-run ]]; then
        dry_run=true
      fi
    done
    if [[ $dry_run == true ]]; then
      run_application "$@"
    else
      maintenance run_application "$@" --flush
    fi
    ;;
  status)
    show_status "${@:2}"
    ;;
  *)
    echo "Unknown or missing command. Run 'noctalia-i18n-watcher --help'." >&2
    exit 2
    ;;
esac
