#!/usr/bin/env bash
# Removes GDrive Sync. Your files in Google Drive are never touched.
#   ./uninstall.sh          keep settings, the file cache and the Google login (reinstall resumes)
#   ./uninstall.sh --purge  also delete settings, cache, logs and revoke + delete the Google login
set -euo pipefail

APP=gdrive-sync
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
VENV="$DATA_HOME/$APP/venv"

if [[ "${1:-}" == "--purge" && -x "$VENV/bin/python" ]]; then
  pending=$("$VENV/bin/python" -c '
from gdrive_sync.config import DB_FILE
from gdrive_sync.store import Store
from gdrive_sync.engine import PENDING_SQL
print(Store(DB_FILE).count(f"WHERE {PENDING_SQL}") if DB_FILE.exists() else 0)' 2>/dev/null || echo 0)
  if [[ "$pending" != "0" ]]; then
    echo "Warning: $pending change(s) have not been uploaded to Drive yet and would be lost."
    read -r -p "Purge anyway? [y/N] " reply
    [[ "$reply" =~ ^[Yy] ]] || exit 1
  fi
fi

systemctl --user disable --now "$APP.service" 2>/dev/null || true   # stopping also unmounts the folder
rm -f "$CONFIG_HOME/systemd/user/$APP.service"
systemctl --user daemon-reload

rm -f "$DATA_HOME/applications/$APP.desktop" "$DATA_HOME/applications/$APP-link.desktop"
rm -f "$DATA_HOME/mime/packages/$APP.xml"
update-mime-database "$DATA_HOME/mime" >/dev/null 2>&1 || true
rm -f "$DATA_HOME"/icons/hicolor/scalable/emblems/emblem-gdrive-*.svg
rm -f "$DATA_HOME/nautilus-python/extensions/gdrive_sync_nautilus.py"
rm -f "$HOME/.local/bin/gdrive-sync"

if [[ "${1:-}" == "--purge" ]]; then
  if [[ -x "$VENV/bin/python" ]]; then
    "$VENV/bin/python" -c '
from gdrive_sync.config import Config
from gdrive_sync.auth import GoogleAuth
GoogleAuth(Config()).disconnect()' 2>/dev/null || true
  fi
  rm -rf "$CONFIG_HOME/$APP" "$DATA_HOME/$APP"
  echo "GDrive Sync removed, including settings, cache and the stored login."
else
  rm -rf "$VENV"
  echo "GDrive Sync removed. Settings and cache kept (use --purge to delete them)."
fi
echo "Run 'nautilus -q' to unload the Files integration."
