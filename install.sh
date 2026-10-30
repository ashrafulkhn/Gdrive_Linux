#!/usr/bin/env bash
# Installs (or upgrades) GDrive Sync for the current user and enables autostart at login.
# No root needed, except for the optional one-time install of python3-nautilus (for icons in Files).
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
EMBLEM_DIR="$DATA_HOME/icons/hicolor/scalable/emblems"
EXT_DIR="$DATA_HOME/nautilus-python/extensions"
SYNC_DIR="${GDRIVE_SYNC_DIR:-$HOME/GoogleDrive}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mNote:\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }
ask() { [[ -t 0 ]] || return 1; read -r -p "$1 [y/N] " reply; [[ "$reply" =~ ^[Yy] ]]; }

[[ $EUID -ne 0 ]] || die "Run this as your normal user, not root."
command -v python3 >/dev/null || die "python3 is required (sudo apt install python3)."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 or newer is required."
python3 -c 'import venv, ensurepip' 2>/dev/null || die "python3-venv is missing: sudo apt install python3-venv"
command -v fusermount3 >/dev/null || die "FUSE 3 is missing: sudo apt install fuse3"
[[ -e /dev/fuse ]] || die "/dev/fuse is not available on this system."

if systemctl --user is-active --quiet "$APP.service"; then
  say "Stopping the running service for the upgrade"
  systemctl --user stop "$APP.service"
fi

# The Drive folder becomes a mount point, so it must be empty.
if [[ -d "$SYNC_DIR" ]] && ! mountpoint -q "$SYNC_DIR" && [[ -n "$(ls -A "$SYNC_DIR" 2>/dev/null)" ]]; then
  die "$SYNC_DIR is not empty. Move its contents elsewhere first: your Drive will appear in this folder."
fi

umask 077
say "Creating virtual environment in $VENV"
mkdir -p "$PREFIX"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
say "Installing GDrive Sync and its dependencies"
"$VENV/bin/pip" install --quiet --upgrade "$SRC_DIR"
rm -rf "$SRC_DIR/build" "$SRC_DIR"/*.egg-info
rm -f "$PREFIX"/state.db*   # sync state of the old full-mirror version
install -m 644 "$SRC_DIR/gdrive_sync/static/icon.svg" "$ICON"

fill() { sed -e "s|@VENV@|$VENV|g" -e "s|@ICON@|$ICON|g" -e "s|@SRC_DIR@|$SRC_DIR|g" "$1"; }

say "Installing systemd user service (autostart at login)"
mkdir -p "$UNIT_DIR"
fill "$SRC_DIR/packaging/gdrive-sync.service" > "$UNIT_DIR/$APP.service"
chmod 644 "$UNIT_DIR/$APP.service"

say "Adding app launcher, Google Docs link handler and status icons"
mkdir -p "$APPS_DIR" "$DATA_HOME/mime/packages" "$EMBLEM_DIR" "$EXT_DIR"
fill "$SRC_DIR/packaging/gdrive-sync.desktop" > "$APPS_DIR/$APP.desktop"
fill "$SRC_DIR/packaging/gdrive-sync-link.desktop" > "$APPS_DIR/$APP-link.desktop"
chmod 644 "$APPS_DIR/$APP.desktop" "$APPS_DIR/$APP-link.desktop"
install -m 644 "$SRC_DIR/packaging/gdrive-sync-mime.xml" "$DATA_HOME/mime/packages/$APP.xml"
install -m 644 "$SRC_DIR"/gdrive_sync/static/emblems/emblem-gdrive-*.svg "$EMBLEM_DIR/"
install -m 644 "$SRC_DIR/packaging/nautilus/gdrive_sync_nautilus.py" "$EXT_DIR/"
update-mime-database "$DATA_HOME/mime" >/dev/null 2>&1 || true
update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
xdg-mime default "$APP-link.desktop" application/x-gdrive-sync-link 2>/dev/null || true
touch "$DATA_HOME/icons/hicolor" 2>/dev/null || true

mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/gdrive-sync" "$HOME/.local/bin/gdrive-sync"

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
systemctl --user enable "$APP.service" >/dev/null 2>&1
systemctl --user restart "$APP.service"
for _ in $(seq 1 30); do
  (echo > /dev/tcp/127.0.0.1/8642) 2>/dev/null && break
  sleep 0.5
done

# Status icons in Files need the Nautilus Python bindings (one-time, needs sudo).
NAUTILUS_RESTART=0
if command -v nautilus >/dev/null; then
  if ! dpkg -s python3-nautilus >/dev/null 2>&1; then
    warn "Status icons in Files need the package python3-nautilus."
    if ask "Install it now with sudo apt?"; then
      sudo apt-get install -y python3-nautilus && NAUTILUS_RESTART=1
    else
      warn "Later run: sudo apt install python3-nautilus && nautilus -q"
    fi
  else
    NAUTILUS_RESTART=1
  fi
  if [[ $NAUTILUS_RESTART -eq 1 ]] && pgrep -x nautilus >/dev/null; then
    if ask "Restart Files now to show the status icons? (open Files windows will close)"; then
      nautilus -q || true
    else
      warn "Run 'nautilus -q' later to load the status icons."
    fi
  fi
fi

say "Done. Opening the dashboard…"
"$VENV/bin/gdrive-sync" open || true
cat <<EOF

  Dashboard:    http://127.0.0.1:8642   (also in your app menu as "GDrive Sync")
  Drive folder: $SYNC_DIR   (files download when you open them)
  Logs:         journalctl --user -u $APP -f
  Uninstall:    $SRC_DIR/uninstall.sh

EOF
