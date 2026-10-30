"""Command line entry point.

    gdrive-sync run             start the sync service and web UI (used by systemd)
    gdrive-sync open            open the dashboard in the browser
    gdrive-sync open-link FILE  open a .gdoc/.gsheet/... link file in the browser
    gdrive-sync reset-password  forget the dashboard password
    gdrive-sync unmount         unmount the Drive folder
"""

import argparse
import errno
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
import threading
import time
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

EXIT_EJECTED = 75  # must match SuccessExitStatus=/RestartPreventExitStatus= in the systemd unit
EJECTED_MSG = ("The Drive folder was unmounted (Eject in the file manager?). Stopping until the next "
               "login, or until you open GDrive Sync from the app menu.")

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
    logging.getLogger("mfusepy").setLevel(logging.WARNING)


def unmount(path):
    exe = shutil.which("fusermount3") or shutil.which("fusermount") or "fusermount3"
    return subprocess.run([exe, "-u", "-z", str(path)], capture_output=True).returncode == 0


def prepare_mountpoint(path):
    """Create the Drive folder, clearing a stale mount left behind by a crash."""
    try:
        os.stat(path)
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700)
    except OSError as e:
        if e.errno != errno.ENOTCONN:  # "Transport endpoint is not connected"
            raise
        unmount(path)
    if os.path.ismount(path):
        unmount(path)  # we hold the single-instance lock, so any mount here is stale
    if any(os.scandir(path)):
        raise OSError(
            errno.ENOTEMPTY,
            f"{path} is not empty. Move its contents somewhere else: your Drive appears in this folder.",
        )
    os.chmod(path, 0o700)


def watch_mount(path, was_mounted, on_ejected, interval=3.0):
    """Notice when the Drive folder is unmounted behind our back.

    "Eject"/"Unmount" in the file manager detaches the folder lazily: the kernel keeps
    the filesystem alive (invisibly) while any program still has a file open in it, so
    the FUSE loop never ends and nothing would ever mount the folder again.
    """
    was_mounted.wait()
    while True:
        time.sleep(interval)
        try:
            mounted = os.path.ismount(path)
        except OSError:
            mounted = False
        if not mounted:
            on_ejected()
            return


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

    from waitress import create_server

    from .auth import GoogleAuth
    from .config import CACHE_DIR, DB_FILE
    from .drive import DriveClient
    from .engine import Engine
    from .store import Store
    from .web import create_app

    config = Config()
    if not config.get("ui_password_hash"):
        read_or_create_secret(SETUP_TOKEN_FILE)
        log.info("First run: open the dashboard with 'gdrive-sync open' to finish setup")

    auth = GoogleAuth(config)
    engine = Engine(config, auth, DriveClient(auth), Store(DB_FILE), CACHE_DIR)
    server = create_server(
        create_app(config, auth, engine), host=HOST, port=config.port, threads=6,
        ident="gdrive-sync", clear_untrusted_proxy_headers=True,
    )
    threading.Thread(target=server.run, name="web", daemon=True).start()
    log.info("Dashboard: http://%s:%d", HOST, config.port)

    mountpoint = config.sync_dir
    try:
        prepare_mountpoint(mountpoint)
        from mfusepy import FUSE

        from .vfs import DriveFS
    except (OSError, ImportError) as e:
        log.error("Cannot mount the Drive folder: %s", e)
        engine.mount_error = str(e)
        engine.start()
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        threading.Event().wait()  # keep the dashboard up so the problem is visible
        return 1

    engine.start()
    fs = DriveFS(engine)
    was_mounted = threading.Event()
    fs.init = lambda path: (setattr(engine, "mounted", True), was_mounted.set())

    def ejected():
        log.warning(EJECTED_MSG)
        engine.mounted = False
        engine.stop()
        server.close()
        logging.shutdown()
        # libfuse would wait forever for the detached mount; exit without restart (see the unit)
        os._exit(EXIT_EJECTED)

    threading.Thread(target=watch_mount, args=(mountpoint, was_mounted, ejected), name="mount-watch",
                     daemon=True).start()
    log.info("Mounting Google Drive at %s", mountpoint)
    ejected_outside = False
    try:
        # libfuse handles SIGTERM/SIGINT itself and unmounts cleanly
        FUSE(fs, str(mountpoint), foreground=True, nothreads=False,
             fsname="gdrive-sync", subtype="gdrive", default_permissions=True)
        # returning without an error means someone unmounted the folder from outside
        ejected_outside = was_mounted.is_set()
    except RuntimeError as e:
        # libfuse reports "stopped by a signal" (systemd stop/restart) as error 8
        if not was_mounted.is_set():
            log.error("The Drive folder could not be mounted (libfuse error %s). Is FUSE available?", e)
            return 1
    finally:
        engine.mounted = False
        engine.stop()
        server.close()
        log.info("Drive folder unmounted")
    if ejected_outside:
        log.warning(EJECTED_MSG)
        return EXIT_EJECTED
    return 0


def cmd_unmount(args):
    return 0 if unmount(Config().sync_dir) else 1


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
        print("Starting GDrive Sync (this also mounts your Drive folder)…")
        subprocess.run(["systemctl", "--user", "start", "gdrive-sync.service"], capture_output=True)
        for _ in range(30):
            if server_up(config.port):
                break
            time.sleep(0.5)
        else:
            print("The service did not start. See: journalctl --user -u gdrive-sync", file=sys.stderr)
    print("Opening the dashboard in your browser. If no window appears, paste this link into any browser:")
    print(f"  {url}")
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
    sub.add_parser("unmount", help="unmount the Drive folder (used when the service stops)")
    args = parser.parse_args(argv)
    handlers = {"open": cmd_open, "open-link": cmd_open_link, "reset-password": cmd_reset_password,
                "unmount": cmd_unmount}
    if args.cmd is None:
        args.verbose = False
    return handlers.get(args.cmd, cmd_run)(args)


if __name__ == "__main__":
    sys.exit(main())
