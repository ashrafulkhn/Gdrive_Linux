#!/usr/bin/env bash
# Installs GDrive Sync for the current user and enables autostart at login.
# No root needed: everything goes under ~/.local and ~/.config.
set -euo pipefail

APP=gdrive-sync
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
PREFIX="$DATA_HOME/$APP"
VENV="$PREFIX/venv"
UNIT_DIR="$CONFIG_HOME/systemd/user"
APPS_DIR="$DATA_HOME/applications"
ICON="$PREFIX/icon.svg"
SYNC_DIR="${GDRIVE_SYNC_DIR:-$HOME/GoogleDrive}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -ne 0 ]] || die "Run this as your normal user, not root."
command -v python3 >/dev/null || die "python3 is required (sudo apt install python3)."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 or newer is required."
python3 -c 'import venv, ensurepip' 2>/dev/null || die "python3-venv is missing: sudo apt install python3-venv"

umask 077
say "Creating virtual environment in $VENV"
mkdir -p "$PREFIX"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
say "Installing GDrive Sync and its dependencies"
"$VENV/bin/pip" install --quiet --upgrade "$SRC_DIR"
install -m 644 "$SRC_DIR/gdrive_sync/static/icon.svg" "$ICON"

fill() { sed -e "s|@VENV@|$VENV|g" -e "s|@ICON@|$ICON|g" -e "s|@SRC_DIR@|$SRC_DIR|g" "$1"; }

say "Installing systemd user service (autostart at login)"
mkdir -p "$UNIT_DIR"
fill "$SRC_DIR/packaging/gdrive-sync.service" > "$UNIT_DIR/$APP.service"
chmod 644 "$UNIT_DIR/$APP.service"

say "Adding app launcher and Google Docs link handler"
mkdir -p "$APPS_DIR" "$DATA_HOME/mime/packages"
fill "$SRC_DIR/packaging/gdrive-sync.desktop" > "$APPS_DIR/$APP.desktop"
fill "$SRC_DIR/packaging/gdrive-sync-link.desktop" > "$APPS_DIR/$APP-link.desktop"
chmod 644 "$APPS_DIR/$APP.desktop" "$APPS_DIR/$APP-link.desktop"
install -m 644 "$SRC_DIR/packaging/gdrive-sync-mime.xml" "$DATA_HOME/mime/packages/$APP.xml"
update-mime-database "$DATA_HOME/mime" >/dev/null 2>&1 || true
update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
xdg-mime default "$APP-link.desktop" application/x-gdrive-sync-link 2>/dev/null || true

mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/gdrive-sync" "$HOME/.local/bin/gdrive-sync"

# Sync folder, private to this user, bookmarked in the Files sidebar
mkdir -p "$SYNC_DIR" && chmod 700 "$SYNC_DIR"
if [[ "$SYNC_DIR" != "$HOME/GoogleDrive" ]]; then
  "$VENV/bin/python" - "$SYNC_DIR" <<'EOF'
import sys
from gdrive_sync.config import Config, ensure_dirs
ensure_dirs()
Config().update(sync_dir=sys.argv[1])
EOF
fi
BOOKMARKS="$CONFIG_HOME/gtk-3.0/bookmarks"
mkdir -p "$(dirname "$BOOKMARKS")"
touch "$BOOKMARKS"
grep -qF "file://$SYNC_DIR" "$BOOKMARKS" || echo "file://$SYNC_DIR Google Drive" >> "$BOOKMARKS"

say "Starting the service"
systemctl --user daemon-reload
systemctl --user enable --now "$APP.service"

for _ in $(seq 1 20); do
  (echo > /dev/tcp/127.0.0.1/8642) 2>/dev/null && break
  sleep 0.5
done

say "Done. Opening the dashboard to finish setup…"
"$VENV/bin/gdrive-sync" open || true
cat <<EOF

  Dashboard:   http://127.0.0.1:8642   (also in your app menu as "GDrive Sync")
  Sync folder: $SYNC_DIR
  Logs:        journalctl --user -u $APP -f
  Uninstall:   $SRC_DIR/uninstall.sh

EOF
