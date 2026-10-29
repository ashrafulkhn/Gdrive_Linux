#!/usr/bin/env bash
# Removes GDrive Sync. Your synced files are never touched.
#   ./uninstall.sh          keep settings and sync state (reinstall resumes where it left off)
#   ./uninstall.sh --purge  also delete settings, sync state, logs and the stored Google login
set -euo pipefail

APP=gdrive-sync
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"

systemctl --user disable --now "$APP.service" 2>/dev/null || true
rm -f "$CONFIG_HOME/systemd/user/$APP.service"
systemctl --user daemon-reload

rm -f "$DATA_HOME/applications/$APP.desktop" "$DATA_HOME/applications/$APP-link.desktop"
rm -f "$DATA_HOME/mime/packages/$APP.xml"
update-mime-database "$DATA_HOME/mime" >/dev/null 2>&1 || true
rm -f "$HOME/.local/bin/gdrive-sync"

if [[ "${1:-}" == "--purge" ]]; then
  if [[ -x "$DATA_HOME/$APP/venv/bin/python" ]]; then
    # revoke the Google token and remove it from the keyring
    "$DATA_HOME/$APP/venv/bin/python" -c '
from gdrive_sync.config import Config
from gdrive_sync.auth import GoogleAuth
GoogleAuth(Config()).disconnect()' 2>/dev/null || true
  fi
  rm -rf "$CONFIG_HOME/$APP" "$DATA_HOME/$APP"
  echo "GDrive Sync removed, including settings and the stored login."
else
  rm -rf "$DATA_HOME/$APP/venv"
  echo "GDrive Sync removed. Settings kept in $CONFIG_HOME/$APP (use --purge to delete)."
fi
echo "Your synced files were not touched."
