"""Two-way sync engine.

The database records the last state on which the local folder and Drive
agreed. Each pass compares one side against that record:

* remote pass - consumes the Drive Changes feed and applies new, modified,
  renamed/moved and removed items to disk;
* local pass  - rescans the folder (triggered by inotify, plus a periodic full
  scan) and pushes creations, edits, renames/moves (detected by inode) and
  deletions to Drive.

When both sides changed the same file, the local version is kept as a
"conflict" copy next to the Drive version, so nothing is ever overwritten.
Deletions go to the Drive trash or the desktop Trash, never straight to
oblivion.
"""

import fnmatch
import hashlib
import json
import logging
import os
import secrets
import shutil
import socket
import stat
import subprocess
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

import requests

from .auth import AuthError
from .config import DATA_DIR
from .db import Entry
from .drive import FOLDER_MIME, SHORTCUT_MIME, DriveError, NotFound

log = logging.getLogger(__name__)

NATIVE_EXT = {
    "application/vnd.google-apps.document": ".gdoc",
    "application/vnd.google-apps.spreadsheet": ".gsheet",
    "application/vnd.google-apps.presentation": ".gslides",
    "application/vnd.google-apps.drawing": ".gdraw",
    "application/vnd.google-apps.form": ".gform",
    "application/vnd.google-apps.jam": ".gjam",
    "application/vnd.google-apps.map": ".gmap",
    "application/vnd.google-apps.site": ".gsite",
    "application/vnd.google-apps.script": ".gscript",
}
GENERIC_NATIVE_EXT = ".glink"
LINK_EXTS = tuple(set(NATIVE_EXT.values()) | {GENERIC_NATIVE_EXT})

DEBOUNCE = 2.0        # wait for this much filesystem quiet before scanning
ACCOUNT_REFRESH = 600


class SyncError(Exception):
    """A condition that must stop syncing until the user fixes it."""


@dataclass
class LocalStat:
    is_dir: bool
    inode: int
    mtime_ns: int
    size: int


def lstat_info(path):
    """Stat without following symlinks; only regular files and dirs count."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(st.st_mode):
        return LocalStat(True, st.st_ino, st.st_mtime_ns, 0)
    if stat.S_ISREG(st.st_mode):
        return LocalStat(False, st.st_ino, st.st_mtime_ns, st.st_size)
    return None


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_native(mime):
    return bool(mime) and mime.startswith("application/vnd.google-apps.") and mime not in (FOLDER_MIME, SHORTCUT_MIME)


def native_ext(mime):
    return NATIVE_EXT.get(mime, GENERIC_NATIVE_EXT)


def local_name(remote_name, mime):
    """Map a Drive name to a safe local file name (no '/', '.', '..')."""
    name = remote_name.replace("/", "_").replace("\0", "")
    if name in ("", ".", ".."):
        name = "_" + name
    ext = native_ext(mime) if is_native(mime) else ""
    limit = 255 - len(ext.encode())
    while len(name.encode()) > limit:
        name = name[:-1]
    return name + ext


def remote_name_for(name, mime):
    if is_native(mime) and name.endswith(native_ext(mime)) and len(name) > len(native_ext(mime)):
        return name[: -len(native_ext(mime))]
    return name


def numbered(name, n):
    stem, ext = os.path.splitext(name)
    return f"{stem} ({n}){ext}"


def join(parent, name):
    return f"{parent}/{name}" if parent else name


def split(rel):
    p = PurePosixPath(rel)
    parent = str(p.parent)
    return ("" if parent == "." else parent), p.name


def move_to_trash(path):
    """Move to the freedesktop Trash (restorable from the file manager)."""
    try:
        subprocess.run(["gio", "trash", "--", str(path)], check=True, capture_output=True, timeout=120)
        return
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("gio trash failed for %s (%s); using fallback trash", path, e)
    dest = DATA_DIR / "trash" / datetime.now().strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest / Path(path).name))


class SyncEngine:
    def __init__(self, config, auth, drive, db, watcher=None, trash=move_to_trash):
        self.config = config
        self.auth = auth
        self.drive = drive
        self.db = db
        self.watcher = watcher
        self.trash_local = trash
        self.hostname = socket.gethostname()
        self.settle_seconds = 2.0  # skip files written in the last N seconds

        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._local_dirty = True
        self._last_local_event = 0.0
        self._force = False
        self._reset_requested = False
        self._allow_mass_delete = False
        self._retry_after = {}
        self._account_checked = 0.0

        self.state = "starting"
        self.detail = ""
        self.error = None
        self.last_sync = None
        self.account = None
        self.pending_mass_delete = None
        self.activity = deque(maxlen=300)
        self.conflicts = deque(maxlen=100)

    # ------------------------------------------------------------------ API
    @property
    def root(self):
        return self.config.sync_dir

    def start(self):
        self._thread = threading.Thread(target=self._run, name="sync-engine", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def notify_local_change(self):
        self._local_dirty = True
        self._last_local_event = time.monotonic()

    def sync_now(self):
        self._force = True
        self._wake.set()

    def request_reset(self):
        """Forget sync state; the next pass re-matches everything by checksum."""
        with self._lock:
            self._reset_requested = True
            self.pending_mass_delete = None
            self.account = None
        self.sync_now()

    def confirm_mass_delete(self):
        with self._lock:
            self._allow_mass_delete = True
            self.pending_mass_delete = None
        self.sync_now()

    def status(self):
        with self._lock:
            return {
                "state": self.state,
                "detail": self.detail,
                "error": self.error,
                "last_sync": self.last_sync,
                "account": self.account,
                "pending_mass_delete": self.pending_mass_delete,
                "activity": list(self.activity)[:100],
                "conflicts": list(self.conflicts),
                "files_tracked": self.db.count(),
                "sync_dir": str(self.root),
            }

    # ------------------------------------------------------------- main loop
    def _run(self):
        next_remote = next_full = 0.0
        backoff = 0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if self.config.get("paused"):
                    self._set_state("paused", "Sync is paused")
                elif not self.auth.is_connected():
                    self._set_state("disconnected", "Connect your Google account to start syncing")
                else:
                    if self._reset_requested:
                        self._reset_requested = False
                        self.db.clear()
                    remote_due = self._force or now >= next_remote
                    local_due = (
                        self._force
                        or now >= next_full
                        or (self._local_dirty and now - self._last_local_event >= DEBOUNCE)
                    )
                    if remote_due or local_due:
                        self._force = False
                        self.sync_once(remote=remote_due, local=local_due)
                        if remote_due:
                            next_remote = time.monotonic() + int(self.config.get("poll_interval"))
                        if local_due:
                            next_full = time.monotonic() + int(self.config.get("full_scan_interval"))
                        backoff = 0
            except AuthError as e:
                self._set_state("auth_error", str(e), error=str(e))
            except SyncError as e:
                self._set_state("error", str(e), error=str(e))
                next_remote = next_full = time.monotonic() + 30
            except (requests.RequestException, DriveError, OSError) as e:
                backoff = min(300, max(15, backoff * 2))
                log.warning("Sync cycle failed, retrying in %ss: %s", backoff, e)
                offline = isinstance(e, (requests.ConnectionError, requests.Timeout))
                msg = "Offline - waiting for network" if offline else f"Error: {e}"
                self._set_state("offline" if offline else "error", msg, error=None if offline else str(e))
                next_remote = next_full = time.monotonic() + backoff
            except Exception as e:  # never let the thread die
                log.exception("Unexpected sync error")
                self._set_state("error", f"Unexpected error: {e}", error=str(e))
                next_remote = next_full = time.monotonic() + 60
            self._wake.wait(1.0)
            self._wake.clear()

    def sync_once(self, remote=True, local=True):
        self._prepare_root()
        if self.watcher:
            self.watcher.ensure(self.root)
        if self.db.meta("initialized") != "1":
            self._initial_sync()
            local = True
        elif remote:
            self._remote_pass()
        if local:
            self._local_pass()
        if time.time() - self._account_checked > ACCOUNT_REFRESH or self.account is None:
            self._refresh_account()
        self.last_sync = time.time()
        if self.pending_mass_delete:
            self._set_state("attention", "Large deletion is waiting for your confirmation")
        else:
            self._set_state("idle", "Up to date")

    def _prepare_root(self):
        if self.db.meta("sync_dir") not in (None, str(self.root)):
            log.info("Sync folder changed; starting fresh")
            self.db.clear()
        if not self.root.is_dir():
            if self.db.count():
                raise SyncError(
                    f"Sync folder {self.root} is missing (unmounted or deleted?). "
                    "Sync is halted to protect your Drive. Recreate the folder, or use "
                    "'Re-sync from scratch' to download everything again."
                )
            self.root.mkdir(parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            # the folder holds a full copy of your Drive: keep other local users out
            os.chmod(self.root, 0o700)

    def _refresh_account(self):
        about = self.drive.about()
        quota = about.get("storageQuota", {})
        with self._lock:
            self.account = {
                "email": about.get("user", {}).get("emailAddress"),
                "name": about.get("user", {}).get("displayName"),
                "usage": int(quota.get("usage", 0)),
                "limit": int(quota["limit"]) if quota.get("limit") else None,
            }
        self._account_checked = time.time()

    # --------------------------------------------------------------- helpers
    def _set_state(self, state, detail="", error=None):
        with self._lock:
            self.state, self.detail = state, detail
            self.error = error

    def _log(self, kind, path, detail=""):
        log.info("%s %s %s", kind, path, detail)
        with self._lock:
            self.activity.appendleft({"time": time.time(), "kind": kind, "path": path, "detail": detail})

    def _abs(self, rel):
        if not rel:
            return self.root
        parts = PurePosixPath(rel).parts
        if rel.startswith("/") or ".." in parts or "." in parts:
            raise ValueError(f"Refusing unsafe path {rel!r}")
        return self.root / rel

    def _ignored(self, name):
        return any(fnmatch.fnmatchcase(name, p) for p in self.config.get("ignore_patterns"))

    def _unchanged(self, e, st):
        return st.mtime_ns == e.local_mtime_ns and st.size == e.local_size

    def _settling(self, st):
        return time.time_ns() - st.mtime_ns < self.settle_seconds * 1e9

    @staticmethod
    def _remote_content_changed(e, f):
        md5 = f.get("md5Checksum")
        if md5:
            return md5 != e.md5
        return f.get("modifiedTime") != e.remote_mtime

    def _free_path(self, parent_rel, name, own_id=None):
        rel, n = join(parent_rel, name), 1
        while True:
            e = self.db.by_path(rel)
            if e is None or e.id == own_id:
                return rel
            rel = join(parent_rel, numbered(name, n))
            n += 1

    def _record(self, f, parent_id, rel, st=None):
        st = st or lstat_info(self._abs(rel))
        self.db.upsert(
            Entry(
                id=f["id"],
                path=rel,
                parent_id=parent_id,
                name=f["name"],
                is_dir=f["mimeType"] == FOLDER_MIME,
                mime=f["mimeType"],
                md5=f.get("md5Checksum"),
                remote_mtime=f.get("modifiedTime"),
                local_mtime_ns=st.mtime_ns if st else 0,
                local_size=st.size if st else 0,
                inode=st.inode if st else 0,
            )
        )

    def _guard(self, key, fn, *args):
        """Run one item operation; failures are logged and retried later with backoff."""
        now = time.monotonic()
        until, delay = self._retry_after.get(key, (0, 0))
        if now < until:
            return False
        try:
            fn(*args)
            self._retry_after.pop(key, None)
            return True
        except (requests.RequestException, AuthError):
            raise
        except (DriveError, OSError, ValueError) as e:
            delay = min(3600, max(60, delay * 2))
            self._retry_after[key] = (now + delay, delay)
            self._log("error", key, str(e))
            return False

    def _conflict_rename(self, rel, reason):
        parent, name = split(rel)
        stem, ext = os.path.splitext(name)
        if self._abs(rel).is_dir():
            stem, ext = name, ""
        stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
        base = f"{stem} (conflict {self.hostname} {stamp})"
        new_rel, n = join(parent, base + ext), 1
        while os.path.lexists(self._abs(new_rel)) or self.db.by_path(new_rel):
            new_rel = join(parent, f"{base} {n}{ext}")
            n += 1
        os.rename(self._abs(rel), self._abs(new_rel))
        with self._lock:
            self.conflicts.appendleft({"time": time.time(), "path": rel, "copy": new_rel, "reason": reason})
        self._log("conflict", rel, f"{reason}; your local version was saved as '{PurePosixPath(new_rel).name}'")
        self._local_dirty = True
        return new_rel

    def _download(self, f, rel):
        ap = self._abs(rel)
        ap.parent.mkdir(parents=True, exist_ok=True)
        tmp = ap.parent / f".gdsync-{secrets.token_hex(6)}.tmp"
        try:
            self.drive.download(f["id"], tmp, f.get("md5Checksum"))
            if f.get("modifiedTime"):
                ts = datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00")).timestamp()
                os.utime(tmp, (ts, ts))
            os.replace(tmp, ap)
        finally:
            tmp.unlink(missing_ok=True)

    def _write_link(self, f, ap):
        url = f.get("webViewLink") or f"https://drive.google.com/open?id={f['id']}"
        body = {
            "url": url,
            "doc_id": f["id"],
            "resource_id": f"{f['mimeType']}:{f['id']}",
            "email": (self.account or {}).get("email"),
        }
        tmp = ap.parent / f".gdsync-{secrets.token_hex(6)}.tmp"
        tmp.write_text(json.dumps(body))
        os.replace(tmp, ap)

    @staticmethod
    def _is_link_file(ap):
        try:
            if os.path.getsize(ap) > 4096:
                return False
            return "doc_id" in json.loads(Path(ap).read_text())
        except (OSError, ValueError, TypeError):
            return False

    def _materialize(self, f, parent_id, rel):
        """Create the local copy of remote item ``f`` at ``rel`` (not yet tracked)."""
        ap = self._abs(rel)
        mime = f["mimeType"]
        existing = lstat_info(ap)
        if existing is None and os.path.lexists(ap):  # symlink/fifo in the way
            self._conflict_rename(rel, "a non-regular file was in the way")
        if mime == FOLDER_MIME:
            if existing and not existing.is_dir:
                self._conflict_rename(rel, "a folder with this name exists in Drive")
                existing = None
            if not existing:
                ap.mkdir(exist_ok=True)
                self._log("down", rel + "/")
        elif is_native(mime):
            if existing and (existing.is_dir or not self._is_link_file(ap)):
                self._conflict_rename(rel, "a Google Docs file with this name exists in Drive")
            self._write_link(f, ap)
            self._log("down", rel, "link to Google Docs file")
        else:
            if existing:
                if (
                    not existing.is_dir
                    and existing.size == int(f.get("size", -1))
                    and file_md5(ap) == f.get("md5Checksum")
                ):
                    self._record(f, parent_id, rel)
                    return
                self._conflict_rename(rel, "a different file with this name exists in Drive")
            self._download(f, rel)
            self._log("down", rel)
        self._record(f, parent_id, rel)

    def _scan(self, sub=""):
        out = {}
        for dirpath, dirnames, filenames in os.walk(self._abs(sub)):
            rel_dir = os.path.relpath(dirpath, self.root)
            rel_dir = "" if rel_dir == "." else rel_dir
            keep = []
            for names, is_dir in ((dirnames, True), (filenames, False)):
                for n in names:
                    if self._ignored(n):
                        continue
                    try:
                        n.encode("utf-8")
                    except UnicodeEncodeError:
                        log.warning("Skipping non-UTF-8 file name in %s", dirpath)
                        continue
                    rel = join(rel_dir, n)
                    st = lstat_info(os.path.join(dirpath, n))
                    if st is None or st.is_dir != is_dir:
                        continue  # symlinks, sockets, fifos are never synced
                    out[rel] = st
                    if is_dir:
                        keep.append(n)
            dirnames[:] = keep
        return out

    def _has_local_changes(self, rel):
        tracked = {e.path: e for e in self.db.subtree(rel)}
        for item, st in self._scan(rel).items():
            e = tracked.get(item)
            if e is None:
                return True
            if not st.is_dir and not is_native(e.mime) and not self._unchanged(e, st):
                return True
        return False

    # ---------------------------------------------------------- initial sync
    def _initial_sync(self):
        self._set_state("syncing", "Listing files in Google Drive…")
        token = self.drive.start_page_token()
        root_id = self.drive.root_id()
        files = self.drive.list_all()
        self.db.clear()
        self.db.set_meta("root_id", root_id)
        self.db.set_meta("sync_dir", str(self.root))

        children = defaultdict(list)
        for f in files:
            if f.get("parents") and f["mimeType"] != SHORTCUT_MIME:
                children[f["parents"][0]].append(f)
        ordered, queue = [], deque([(root_id, "")])
        while queue:
            pid, prel = queue.popleft()
            taken = set()
            for f in sorted(children.get(pid, []), key=lambda f: (f["mimeType"] != FOLDER_MIME, f["name"], f["id"])):
                base = local_name(f["name"], f["mimeType"])
                if self._ignored(base):
                    continue
                name, n = base, 1
                while name in taken:
                    name, n = numbered(base, n), n + 1
                taken.add(name)
                rel = join(prel, name)
                ordered.append((f, pid, rel))
                if f["mimeType"] == FOLDER_MIME:
                    queue.append((f["id"], rel))

        total = len(ordered)
        for i, (f, pid, rel) in enumerate(ordered, 1):
            self._set_state("syncing", f"Initial sync {i}/{total}: {rel}")
            if pid != root_id and self.db.get(pid) is None:
                continue  # parent folder could not be created
            self._guard(rel, self._materialize, f, pid, rel)
        self.db.set_meta("page_token", token)
        self.db.set_meta("initialized", "1")

    # ----------------------------------------------------------- remote pass
    def _remote_pass(self):
        changes, new_token = self.drive.changes(self.db.meta("page_token"))
        if changes:
            self._set_state("syncing", f"Applying {len(changes)} change(s) from Drive…")
        latest = {}
        for c in changes:
            latest.pop(c["fileId"], None)
            latest[c["fileId"]] = c
        pending, retry = list(latest.values()), False
        while pending:
            deferred = []
            for c in pending:
                try:
                    if not self._apply_remote(c):
                        deferred.append(c)
                except (requests.RequestException, AuthError):
                    raise
                except (DriveError, OSError, ValueError) as e:
                    name = (c.get("file") or {}).get("name", c["fileId"])
                    self._log("error", name, str(e))
                    # transient problems (disk full, server errors) are retried from the same token
                    retry = retry or not isinstance(e, DriveError) or (e.status or 500) >= 500
            if len(deferred) == len(pending):
                break
            pending = deferred
        for c in pending:  # parent is not in My Drive (e.g. moved to a shared drive)
            self._remote_gone(c["fileId"])
        if not retry:
            self.db.set_meta("page_token", new_token)

    def _apply_remote(self, c):
        """Apply one change; returns False if its parent folder isn't known yet."""
        fid, f = c["fileId"], c.get("file")
        root_id = self.db.meta("root_id")
        if fid == root_id:
            return True
        if c.get("removed") or not f or f.get("trashed") or f["mimeType"] == SHORTCUT_MIME or not f.get("parents"):
            self._remote_gone(fid)
            return True
        pid = f["parents"][0]
        if pid == root_id:
            parent_rel = ""
        else:
            pe = self.db.get(pid)
            if pe is None or not pe.is_dir:
                return False
            parent_rel = pe.path
        name = local_name(f["name"], f["mimeType"])
        if self._ignored(name):
            self._remote_gone(fid)
            return True

        e = self.db.get(fid)
        if e is not None and e.is_dir != (f["mimeType"] == FOLDER_MIME):
            self._remote_gone(fid)
            e = None
        if e is None:
            self._materialize(f, pid, self._free_path(parent_rel, name))
            return True
        if e.parent_id != pid or e.name != f["name"]:
            e = self._remote_move(e, f, pid, parent_rel, name)
        if not e.is_dir and not is_native(e.mime) and self._remote_content_changed(e, f):
            self._remote_modified(e, f)
        return True

    def _remote_move(self, e, f, pid, parent_rel, name):
        new_rel = self._free_path(parent_rel, name, own_id=e.id)
        old_rel = e.path
        if new_rel != old_rel:
            src, dst = self._abs(old_rel), self._abs(new_rel)
            if os.path.lexists(dst):
                self._conflict_rename(new_rel, "an untracked item was in the way of a rename from Drive")
            if os.path.lexists(src):
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.rename(src, dst)
            self.db.rename_subtree(old_rel, new_rel)
            self._log("move", old_rel, f"→ {new_rel} (renamed in Drive)")
        e = self.db.get(e.id)
        e.parent_id, e.name = pid, f["name"]
        st = lstat_info(self._abs(new_rel))
        if st:
            e.inode = st.inode
        self.db.upsert(e)
        return e

    def _remote_modified(self, e, f):
        ap = self._abs(e.path)
        st = lstat_info(ap)
        if st and st.is_dir:
            self._conflict_rename(e.path, "a local folder replaced a file that changed in Drive")
        elif st and not self._unchanged(e, st):
            local_md5 = file_md5(ap)
            if local_md5 == f.get("md5Checksum"):
                self._record(f, e.parent_id, e.path)
                return
            if local_md5 != e.md5:
                self._conflict_rename(e.path, "edited both here and in Drive")
        self._download(f, e.path)
        self._record(f, e.parent_id, e.path)
        self._log("down", e.path, "updated from Drive")

    def _remote_gone(self, fid):
        e = self.db.get(fid)
        if e is None:
            return
        ap = self._abs(e.path)
        st = lstat_info(ap)
        if st is None:
            pass
        elif e.is_dir:
            if self._has_local_changes(e.path):
                self._log("info", e.path, "removed from Drive but has local changes; uploading it again")
                self._local_dirty = True
            else:
                self.trash_local(ap)
                self._log("delete", e.path + "/", "removed from Drive; moved to Trash")
        elif not is_native(e.mime) and not self._unchanged(e, st) and file_md5(ap) != e.md5:
            self._log("info", e.path, "removed from Drive but edited here; uploading it again")
            self._local_dirty = True
        else:
            self.trash_local(ap)
            self._log("delete", e.path, "removed from Drive; moved to Trash")
        self.db.delete_subtree(e.path)

    # ------------------------------------------------------------ local pass
    def _local_pass(self):
        self._local_dirty = False
        self._set_state("syncing", "Scanning local folder…")
        scan = self._scan()
        entries = self.db.all()
        tracked = {e.path for e in entries}
        untracked_by_inode = {}
        for rel, st in scan.items():
            if rel not in tracked:
                untracked_by_inode.setdefault(st.inode, rel)
        missing = sorted((e for e in entries if e.path not in scan), key=lambda e: (e.path.count("/"), e.path))

        # 1. renames / moves (same inode now at an untracked path)
        moved_to, failed = set(), set()
        for e0 in missing:
            e = self.db.get(e0.id)
            if e is None or e.path in scan:
                continue
            cand = untracked_by_inode.get(e.inode)
            if not cand or cand in moved_to or self.db.by_path(cand):
                continue
            st = scan[cand]
            if st.is_dir != e.is_dir or (not e.is_dir and not self._unchanged(e, st)):
                continue
            moved_to.add(cand)
            if not self._guard(cand, self._local_move, e, cand):
                failed.add(e.id)

        # 2. deletions, with a guard against mass deletion (e.g. folder emptied by accident)
        gone = []
        for e0 in missing:
            e = self.db.get(e0.id)
            if e is None or e.path in scan or e.id in failed:
                continue
            if any(e.path.startswith(g.path + "/") for g in gone):
                continue  # covered by trashing its parent folder
            gone.append(e)
        n_items = sum(len(self.db.subtree(g.path)) if g.is_dir else 1 for g in gone)
        threshold = int(self.config.get("mass_delete_threshold"))
        if n_items > threshold and not self._allow_mass_delete:
            with self._lock:
                self.pending_mass_delete = {"count": n_items, "examples": [g.path for g in gone[:10]]}
            log.warning("Holding back deletion of %d items until confirmed in the UI", n_items)
        else:
            with self._lock:
                self._allow_mass_delete = False
                self.pending_mass_delete = None
            for e in gone:
                self._guard(e.path, self._local_delete, e)

        # 3. new files and folders, parents first
        for rel in sorted(scan, key=lambda r: (r.count("/"), r)):
            if self.db.by_path(rel) is None:
                self._guard(rel, self._local_create, rel, scan[rel])

        # 4. edits
        for rel, st in scan.items():
            if st.is_dir:
                continue
            e = self.db.by_path(rel)
            if e and not self._unchanged(e, st):
                self._guard(rel, self._local_modified, e, st)

    def _ensure_remote_dir(self, rel):
        if not rel:
            return self.db.meta("root_id")
        e = self.db.by_path(rel)
        if e:
            if not e.is_dir:
                raise DriveError(f"'{rel}' is a file in Drive, not a folder")
            return e.id
        parent_rel, name = split(rel)
        pid = self._ensure_remote_dir(parent_rel)
        f = self.drive.create_folder(name, pid)
        self._record(f, pid, rel)
        self._log("up", rel + "/")
        return f["id"]

    def _local_move(self, e, new_rel):
        parent_rel, new_name = split(new_rel)
        pid = self._ensure_remote_dir(parent_rel)
        remote_name = remote_name_for(new_name, e.mime)
        moved = pid != e.parent_id
        try:
            f = self.drive.update_metadata(
                e.id,
                name=remote_name if remote_name != e.name else None,
                add_parent=pid if moved else None,
                remove_parent=e.parent_id if moved else None,
            )
        except NotFound:
            self.db.delete_subtree(e.path)  # gone remotely: will be uploaded as new
            return
        old = e.path
        self.db.rename_subtree(old, new_rel)
        e = self.db.get(e.id)
        e.name, e.parent_id = f["name"], pid
        e.remote_mtime = f.get("modifiedTime", e.remote_mtime)
        self.db.upsert(e)
        self._log("move", old, f"→ {new_rel}")

    def _local_delete(self, e):
        try:
            self.drive.trash(e.id)
        except NotFound:
            pass
        self.db.delete_subtree(e.path)
        self._log("delete", e.path + ("/" if e.is_dir else ""), "deleted here; moved to Drive trash")

    def _local_create(self, rel, st):
        ap = self._abs(rel)
        if st.is_dir:
            self._ensure_remote_dir(rel)
            return
        if self._settling(st):
            self._local_dirty = True  # still being written; look again shortly
            return
        parent_rel, name = split(rel)
        if name.endswith(LINK_EXTS) and self._is_link_file(ap):
            return  # a copied Google Docs link file is not real content
        pid = self._ensure_remote_dir(parent_rel)
        f = self.drive.upload_new(ap, name, pid)
        self._record(f, pid, rel, st)
        self._log("up", rel)

    def _local_modified(self, e, st):
        ap = self._abs(e.path)
        if is_native(e.mime):
            e.local_mtime_ns, e.local_size, e.inode = st.mtime_ns, st.size, st.inode
            self.db.upsert(e)
            return
        if self._settling(st):
            self._local_dirty = True
            return
        if file_md5(ap) == e.md5:  # touched but not changed
            e.local_mtime_ns, e.local_size, e.inode = st.mtime_ns, st.size, st.inode
            self.db.upsert(e)
            return
        try:
            remote = self.drive.get(e.id)
        except NotFound:
            remote = None
        if remote is None or remote.get("trashed"):
            self.db.delete(e.id)  # re-upload as a new file
            self._local_create(e.path, st)
            return
        if self._remote_content_changed(e, remote):
            self._conflict_rename(e.path, "edited both here and in Drive")
            self._download(remote, e.path)
            self._record(remote, e.parent_id, e.path)
            self._log("down", e.path, "updated from Drive")
            return
        f = self.drive.upload_update(e.id, ap)
        self._record(f, e.parent_id, e.path, st)
        self._log("up", e.path, "updated")
