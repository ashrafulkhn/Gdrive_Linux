"""Paths, persistent settings and helpers for writing private files."""

import json
import os
import secrets
import threading
from pathlib import Path

APP_NAME = "gdrive-sync"
DEFAULT_PORT = 8642
HOST = "127.0.0.1"  # never bind to anything but loopback

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME

CONFIG_FILE = CONFIG_DIR / "config.json"
SECRET_KEY_FILE = CONFIG_DIR / "secret_key"
SETUP_TOKEN_FILE = CONFIG_DIR / "setup_token"
TOKEN_FILE = CONFIG_DIR / "token.json"  # only used when no system keyring is available
DB_FILE = DATA_DIR / "state.db"
LOG_FILE = DATA_DIR / "gdrive-sync.log"
LOCK_FILE = DATA_DIR / "instance.lock"

DEFAULT_IGNORE = [
    ".gdsync-*",        # our own temporary download files
    "*.swp", "*.swx", "*.swo", "*~",
    ".~lock.*#",        # LibreOffice lock files
    ".goutputstream-*", # GTK atomic-save temp files
    "*.part", "*.crdownload", "*.tmp",
    ".Trash-*", ".directory", ".DS_Store", "Thumbs.db", "desktop.ini",
]

DEFAULTS = {
    "port": DEFAULT_PORT,
    "sync_dir": str(Path.home() / "GoogleDrive"),
    "poll_interval": 30,          # seconds between Drive change checks
    "full_scan_interval": 600,    # seconds between full local rescans
    "ui_password_hash": None,
    "client_id": None,
    "client_secret": None,
    "paused": False,
    "mass_delete_threshold": 50,  # pause and ask if a pass would delete more items than this
    "ignore_patterns": DEFAULT_IGNORE,
}


def ensure_dirs():
    for d in (CONFIG_DIR, DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)


def write_private(path, data):
    """Atomically write ``data`` to ``path`` with 0600 permissions."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode()
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_or_create_secret(path, nbytes=32):
    path = Path(path)
    try:
        value = path.read_text().strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    value = secrets.token_urlsafe(nbytes)
    write_private(path, value)
    return value


class Config:
    def __init__(self, path=CONFIG_FILE):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        try:
            self._data.update(json.loads(self.path.read_text()))
        except FileNotFoundError:
            pass

    def get(self, key):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key))

    def update(self, **values):
        with self._lock:
            self._data.update(values)
            write_private(self.path, json.dumps(self._data, indent=2))

    @property
    def sync_dir(self):
        return Path(self.get("sync_dir")).expanduser()

    @property
    def port(self):
        return int(os.environ.get("GDRIVE_SYNC_PORT") or self.get("port"))
