"""Command line entry point.

    gdrive-sync run             start the sync service and web UI (used by systemd)
    gdrive-sync open            open the dashboard in the browser
    gdrive-sync open-link FILE  open a .gdoc/.gsheet/... link file in the browser
    gdrive-sync reset-password  forget the dashboard password
"""

import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import shutil
import socket
import subprocess
import sys
import webbrowser
from urllib.parse import quote, urlparse

from .config import (
    HOST,
    LOCK_FILE,
    LOG_FILE,
    SECRET_KEY_FILE,
    SETUP_TOKEN_FILE,
    Config,
    ensure_dirs,
    read_or_create_secret,
)

LINK_HOSTS = {"docs.google.com", "drive.google.com", "script.google.com", "sites.google.com", "jamboard.google.com"}


def setup_logging(verbose=False):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    os.chmod(LOG_FILE, 0o600)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("watchdog").setLevel(logging.WARNING)


def cmd_run(args):
    os.umask(0o077)  # everything we create is private to this user
    ensure_dirs()
    setup_logging(args.verbose)
    log = logging.getLogger("gdrive_sync")
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("Another gdrive-sync instance is already running")
        return 1

    from waitress import serve

    from .auth import GoogleAuth
    from .db import StateDB
    from .config import DB_FILE
    from .drive import DriveClient
    from .sync import SyncEngine
    from .watcher import LocalWatcher
    from .web import create_app

    config = Config()
    if not config.get("ui_password_hash"):
        read_or_create_secret(SETUP_TOKEN_FILE)
        log.info("First run: open the dashboard with 'gdrive-sync open' to finish setup")

    auth = GoogleAuth(config)
    drive = DriveClient(auth)
    db = StateDB(DB_FILE)
    engine = SyncEngine(config, auth, drive, db)
    engine.watcher = LocalWatcher(engine, config.get("ignore_patterns"))
    engine.start()

    def shutdown(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    app = create_app(config, auth, engine)
    log.info("Dashboard: http://%s:%d", HOST, config.port)
    try:
        serve(app, host=HOST, port=config.port, threads=4, ident="gdrive-sync", clear_untrusted_proxy_headers=True)
    finally:
        engine.stop()
        if engine.watcher:
            engine.watcher.stop()
    return 0


def launch_browser(url):
    """Open ``url`` in the default browser, detached and with its console noise silenced."""
    for opener in ("xdg-open", "gio"):
        exe = shutil.which(opener)
        if exe:
            args = [exe, url] if opener == "xdg-open" else [exe, "open", url]
            subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
    webbrowser.open(url)


def server_up(port):
    try:
        with socket.create_connection((HOST, port), timeout=1):
            return True
    except OSError:
        return False


def cmd_open(args):
    ensure_dirs()
    config = Config()
    base = f"http://{HOST}:{config.port}"
    if config.get("ui_password_hash"):
        url = base + "/"
    else:
        url = f"{base}/setup?token={quote(read_or_create_secret(SETUP_TOKEN_FILE))}"
    if not server_up(config.port):
        print("The gdrive-sync service is not running. Start it with:\n  systemctl --user start gdrive-sync", file=sys.stderr)
    print(f"Opening {url.split('?')[0]} in your browser…")
    launch_browser(url)
    return 0


def cmd_open_link(args):
    try:
        with open(args.file, "rb") as f:
            data = json.loads(f.read(64 * 1024))
        url = data["url"]
        parsed = urlparse(url)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"Not a valid Google Docs link file: {e}", file=sys.stderr)
        return 1
    # only ever open Google URLs from these files, never arbitrary links
    if parsed.scheme != "https" or parsed.hostname not in LINK_HOSTS:
        print(f"Refusing to open untrusted URL: {url}", file=sys.stderr)
        return 1
    launch_browser(url)
    return 0


def cmd_reset_password(args):
    ensure_dirs()
    config = Config()
    config.update(ui_password_hash=None)
    SECRET_KEY_FILE.unlink(missing_ok=True)  # invalidates all existing sessions
    SETUP_TOKEN_FILE.unlink(missing_ok=True)
    read_or_create_secret(SETUP_TOKEN_FILE)
    print("Dashboard password cleared. Restart the service, then set a new password:")
    print("  systemctl --user restart gdrive-sync && gdrive-sync open")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="gdrive-sync", description="Two-way Google Drive sync for Linux")
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="run the sync service and web UI")
    p_run.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("open", help="open the dashboard in a browser")
    p_link = sub.add_parser("open-link", help="open a .gdoc/.gsheet/... file in the browser")
    p_link.add_argument("file")
    sub.add_parser("reset-password", help="forget the dashboard password")
    args = parser.parse_args(argv)
    handlers = {"open": cmd_open, "open-link": cmd_open_link, "reset-password": cmd_reset_password}
    if args.cmd is None:
        args.verbose = False
    return handlers.get(args.cmd, cmd_run)(args)


if __name__ == "__main__":
    sys.exit(main())
