"""On-demand sync engine behind the virtual Drive folder.

* The whole Drive tree is kept as metadata, so every file and folder is
  visible immediately without downloading anything.
* A file's content is downloaded into the local cache the first time it is
  opened ("hydrated"), and stays there until evicted.
* Pinned ("available offline") files and folders are downloaded ahead of
  time and never evicted. Pinning the root gives a full mirror.
* Local edits, creations, renames and deletions are recorded in the store
  and pushed to Drive by the background worker; Drive changes arrive via the
  Changes API. If a file changed on both sides, the local version is kept
  as a separate "conflict" copy so nothing is lost.
"""

import errno
import fnmatch
import hashlib
import itertools
import json
import logging
import os
import secrets
import shutil
import socket
import threading
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

from .auth import AuthError
from .drive import FOLDER_MIME, SHORTCUT_MIME, DriveError, NotFound, guess_mime
from .store import ROOT

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
NATIVE_SQL = "(mime IS NULL OR mime NOT LIKE 'application/vnd.google-apps.%')"

HEAD_BYTES = 64 * 1024   # small reads at the start of a file (type sniffing) don't download it all
ACCOUNT_REFRESH = 600
PREFETCH_BUDGET = 20     # seconds of pinned downloads per worker cycle, so Drive polling keeps going
PENDING_SQL = "(dirty=1 OR remote_id IS NULL OR moved=1 OR deleted=1) AND ino<>1"

STATUSES = ("cloud", "local", "pinned", "syncing", "error")


class SyncError(Exception):
    """A condition that must stop syncing until the user fixes it."""


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
    ext = native_ext(mime)
    if is_native(mime) and name.endswith(ext) and len(name) > len(ext):
        return name[: -len(ext)]
    return name


def numbered(name, n):
    stem, ext = os.path.splitext(name)
    return f"{stem} ({n}){ext}"


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def time_ns(rfc3339):
    if not rfc3339:
        return time.time_ns()
    return int(datetime.fromisoformat(rfc3339.replace("Z", "+00:00")).timestamp() * 1e9)


def link_bytes(remote_id, mime, link):
    """Content of the .gdoc/.gsheet/... file shown for Google-native documents."""
    url = link or f"https://drive.google.com/open?id={remote_id}"
    return json.dumps({"url": url, "doc_id": remote_id, "resource_id": f"{mime}:{remote_id}"}).encode()


@dataclass
class Handle:
    ino: int
    writable: bool
    fd: int | None = None
    wrote: bool = False
    virtual: bytes | None = None


class Engine:
    def __init__(self, config, auth, drive, store, cache_dir):
        self.config = config
        self.auth = auth
        self.drive = drive
        self.store = store
        store.on_change = self._bump
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.cache_dir, 0o700)
        self.hostname = socket.gethostname()
        self.settle_seconds = 2.0
        self.prefetch_budget = PREFETCH_BUDGET
        self.mounted = False
        self.mount_error = None

        self.gen = 0  # bumped whenever anything that affects a file's status changes
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._handles = {}
        self._fh = itertools.count(1)
        self._node_locks = defaultdict(threading.RLock)
        self._heads = OrderedDict()
        self.downloading = set()
        self.uploading = set()
        self.errors = {}
        self._download_queue = deque()
        self._last_write = {}
        self._local_changed = 0.0
        self._force = False
        self._reset_requested = False
        self._allow_mass_delete = False
        self._retry_after = {}
        self._account_checked = 0.0
        self._status_cache = {}
        self._status_cache_gen = -1
        self._pending_dirs = set()
        self._pending_dirs_gen = -1

        self.state = "starting"
        self.detail = ""
        self.error = None
        self.last_sync = None
        self.account = None
        self.pending_mass_delete = None
        self.activity = deque(maxlen=300)
        self.conflicts = deque(maxlen=100)

    # ================================================================ helpers
    def _bump(self):
        self.gen += 1

    def cache_path(self, ino):
        return self.cache_dir / str(ino)

    def ignored(self, name):
        return any(fnmatch.fnmatchcase(name, p) for p in self.config.get("ignore_patterns"))

    def _touch_local(self):
        self._local_changed = time.monotonic()

    def _set_busy(self, bucket, ino, busy):
        with self._lock:
            (bucket.add if busy else bucket.discard)(ino)
        self._bump()

    def _set_state(self, state, detail="", error=None):
        with self._lock:
            self.state, self.detail, self.error = state, detail, error

    def _log(self, kind, path, detail=""):
        path = path.lstrip("/")
        log.info("%s %s %s", kind, path, detail)
        with self._lock:
            self.activity.appendleft({"time": time.time(), "kind": kind, "path": path, "detail": detail})

    def _unique(self, parent, name, own=None):
        candidate, n = name, 1
        while True:
            hit = self.store.child(parent, candidate)
            if hit is None or hit.ino == own:
                return candidate
            candidate, n = numbered(name, n), n + 1

    def _drop_cache(self, ino):
        self.cache_path(ino).unlink(missing_ok=True)
        with self._lock:
            for key in [k for k in self._heads if k[0] == ino]:
                del self._heads[key]

    def _node_lock(self, ino):
        with self._lock:
            return self._node_locks[ino]

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
            cause = e.__cause__
            if isinstance(cause, (requests.RequestException, AuthError)):
                raise cause
            delay = min(3600, max(60, delay * 2))
            self._retry_after[key] = (now + delay, delay)
            self._log("error", str(key[1]) if isinstance(key, tuple) else str(key), str(e))
            return False

    @staticmethod
    def _content_changed(n, f):
        md5 = f.get("md5Checksum")
        if md5:
            return md5 != n.r_md5
        return f.get("modifiedTime") != n.r_mtime

    def pinned_effective(self, n):
        return n.pinned or any(a.pinned for a in self.store.ancestors(n.ino))

    # ================================================================ handles
    def open_handle(self, ino, writable, fd=None, virtual=None):
        with self._lock:
            fh = next(self._fh)
            self._handles[fh] = Handle(ino, writable, fd, virtual=virtual)
        with self.store.lock:
            self.store.conn.execute("UPDATE nodes SET atime=? WHERE ino=?", (time.time(), ino))
        return fh

    def handle(self, fh):
        h = self._handles.get(fh)
        if h is None:
            raise OSError(errno.EBADF, "bad file handle")
        return h

    def ensure_fd(self, h):
        if h.fd is None:
            n = self.store.get(h.ino)
            if n is None:
                raise OSError(errno.ENOENT, "file is gone")
            if not n.cached:
                self.hydrate(h.ino)
            h.fd = os.open(self.cache_path(h.ino), os.O_RDWR if h.writable else os.O_RDONLY)
        return h.fd

    def note_write(self, h):
        h.wrote = True
        self._last_write[h.ino] = time.monotonic()

    def close_handle(self, fh):
        with self._lock:
            h = self._handles.pop(fh, None)
        if h is None:
            return
        try:
            if h.wrote and h.fd is not None:
                st = os.fstat(h.fd)
                with self.store.lock:
                    n = self.store.get(h.ino)
                    if n is not None and not n.deleted:
                        self.store.update(h.ino, size=st.st_size, mtime_ns=st.st_mtime_ns, cached=1, dirty=1, ver=n.ver + 1)
                self._last_write[h.ino] = time.monotonic()
                self._touch_local()
        finally:
            if h.fd is not None:
                os.close(h.fd)

    def writer_fd(self, ino):
        with self._lock:
            for h in self._handles.values():
                if h.ino == ino and h.writable and h.fd is not None:
                    return h.fd
        return None

    def is_open(self, ino):
        with self._lock:
            return any(h.ino == ino for h in self._handles.values())

    def has_writer(self, ino):
        with self._lock:
            return any(h.ino == ino and h.writable for h in self._handles.values())

    def _retarget(self, old, new):
        with self._lock:
            for h in self._handles.values():
                if h.ino == old:
                    h.ino = new

    # ======================================================= local mutations
    def create_file(self, parent, name):
        with self.store.lock:
            if self.store.child(parent, name):
                raise FileExistsError(errno.EEXIST, name)
            ino = self.store.insert(
                parent=parent, name=name, is_dir=0, mime=guess_mime(name), size=0,
                mtime_ns=time.time_ns(), cached=1, dirty=1, atime=time.time(),
            )
            fd = os.open(self.cache_path(ino), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        self._last_write[ino] = time.monotonic()
        self._touch_local()
        return ino, fd

    def make_dir(self, parent, name):
        with self.store.lock:
            if self.store.child(parent, name):
                raise FileExistsError(errno.EEXIST, name)
            self.store.insert(parent=parent, name=name, is_dir=1, mime=FOLDER_MIME, mtime_ns=time.time_ns())
        self._touch_local()

    def remove(self, n):
        with self.store.lock:
            if n.remote_id is None:
                self.store.delete(n.ino)
            else:
                self.store.update(n.ino, deleted=1, cached=0, dirty=0, moved=0)
            self._drop_cache(n.ino)
        self._touch_local()

    def rename(self, src, parent, name):
        with self.store.lock:
            src = self.store.get(src.ino)
            if src.is_dir and (parent.ino == src.ino or any(a.ino == src.ino for a in self.store.ancestors(parent.ino))):
                raise OSError(errno.EINVAL, "cannot move a folder into itself")
            dst = self.store.child(parent.ino, name)
            if dst is not None and dst.ino == src.ino:
                return
            if dst is not None:
                if dst.is_dir:
                    if not src.is_dir:
                        raise IsADirectoryError(errno.EISDIR, name)
                    if self.store.children(dst.ino):
                        raise OSError(errno.ENOTEMPTY, name)
                    self.remove(dst)
                elif src.is_dir:
                    raise NotADirectoryError(errno.ENOTDIR, name)
                elif src.cached and dst.remote_id and not is_native(src.mime) and not is_native(dst.mime):
                    self._replace_content(src, dst)
                    return
                else:
                    self.remove(dst)
            self.store.update(src.ino, parent=parent.ino, name=name, moved=1 if src.remote_id else 0)
        self._touch_local()

    def _replace_content(self, src, dst):
        """Atomic save (write a temp file, rename it over the original): keep the
        original Drive file, so its sharing settings and version history survive."""
        os.replace(self.cache_path(src.ino), self.cache_path(dst.ino))
        st = os.stat(self.cache_path(dst.ino))
        self.store.update(
            dst.ino, cached=1, dirty=1, size=st.st_size, mtime_ns=st.st_mtime_ns, ver=dst.ver + 1, atime=time.time()
        )
        self._retarget(src.ino, dst.ino)
        if src.remote_id:
            self.store.update(src.ino, deleted=1, cached=0, dirty=0, moved=0)
        else:
            self.store.delete(src.ino)
        self._last_write[dst.ino] = time.monotonic()
        self._touch_local()

    def truncate(self, n, length, fh=None):
        if is_native(n.mime):
            raise PermissionError(errno.EACCES, "Google Docs links are read-only")
        if fh is not None:
            h = self.handle(fh)
            os.ftruncate(self.ensure_fd(h), length)
            self.note_write(h)
            return
        with self._node_lock(n.ino):
            path = self.cache_path(n.ino)
            if length == 0 and not n.cached:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.close(fd)
            else:
                self.hydrate(n.ino)
                os.truncate(path, length)
            st = os.stat(path)
            with self.store.lock:
                cur = self.store.get(n.ino)
                self.store.update(n.ino, size=st.st_size, mtime_ns=st.st_mtime_ns, cached=1, dirty=1, ver=cur.ver + 1)
        self._last_write[n.ino] = time.monotonic()
        self._touch_local()

    def set_mtime(self, n, mtime_ns):
        self.store.update(n.ino, mtime_ns=mtime_ns)
        if n.cached:
            try:
                os.utime(self.cache_path(n.ino), ns=(mtime_ns, mtime_ns))
            except FileNotFoundError:
                pass

    # ============================================================ downloads
    def hydrate(self, ino):
        """Make sure a file's content is in the cache (blocks while downloading)."""
        with self._node_lock(ino):
            for _ in range(3):
                n = self.store.get(ino)
                if n is None or n.deleted:
                    raise FileNotFoundError(errno.ENOENT, "file is gone")
                if n.cached:
                    return
                if n.remote_id is None:
                    self.cache_path(ino).touch(mode=0o600)
                    self.store.update(ino, cached=1)
                    return
                expected = n.r_md5
                tmp = self.cache_dir / f".dl-{ino}-{secrets.token_hex(4)}"
                self._set_busy(self.downloading, ino, True)
                try:
                    self.drive.download(n.remote_id, tmp, expected)
                    with self.store.lock:
                        cur = self.store.get(ino)
                        if cur is None or cur.deleted:
                            raise FileNotFoundError(errno.ENOENT, "file is gone")
                        if cur.cached:
                            return
                        if cur.r_md5 != expected:
                            continue  # changed in Drive while downloading: fetch again
                        os.utime(tmp, ns=(cur.mtime_ns, cur.mtime_ns))
                        os.replace(tmp, self.cache_path(ino))
                        self.store.update(ino, cached=1, size=os.path.getsize(self.cache_path(ino)))
                    self.errors.pop(ino, None)
                    self._log("down", self.store.path_of(ino))
                    return
                except (requests.RequestException, DriveError, AuthError) as e:
                    self.errors[ino] = str(e)
                    raise OSError(errno.EIO, f"download failed: {e}") from e
                finally:
                    tmp.unlink(missing_ok=True)
                    self._set_busy(self.downloading, ino, False)
            raise OSError(errno.EAGAIN, "file keeps changing in Drive; try again")

    def read_head(self, n, offset, size):
        """Serve a small read at the start of a cloud-only file without downloading it all."""
        key = (n.ino, n.r_md5)
        with self._lock:
            data = self._heads.get(key)
        if data is None:
            try:
                data = self.drive.download_range(n.remote_id, 0, HEAD_BYTES - 1)
            except (requests.RequestException, DriveError, AuthError) as e:
                raise OSError(errno.EIO, f"download failed: {e}") from e
            with self._lock:
                self._heads[key] = data
                while len(self._heads) > 64:
                    self._heads.popitem(last=False)
        return data[offset : offset + size]

    # ================================================================ status
    def status_of(self, n):
        if n.ino in self.errors:
            return "error"
        if n.is_dir:
            if n.ino in self._pending_dir_set():
                return "syncing"
            if self.pinned_effective(n):
                return "pinned"
            return "cloud" if self._has_uncached(n.ino) else "local"
        if is_native(n.mime):
            return "cloud"
        if n.remote_id is None and self.ignored(n.name):
            return "local"  # temporary/lock file: kept on this computer only
        if n.ino in self.downloading or n.ino in self.uploading or n.dirty or n.remote_id is None or n.moved:
            return "syncing"
        if n.cached:
            return "pinned" if self.pinned_effective(n) else "local"
        return "cloud"

    def _pending(self):
        nodes = self.store.query(f"WHERE {PENDING_SQL}")
        return [n for n in nodes if not (n.remote_id is None and self.ignored(n.name))]

    def _pending_dir_set(self):
        # recomputed whenever anything changed: the file manager only re-checks
        # icons when the generation moves, so a stale answer would stick
        gen = self.gen
        if self._pending_dirs_gen == gen:
            return self._pending_dirs
        inos = {n.ino for n in self._pending()} | set(self.downloading) | set(self.uploading)
        dirs = set()
        for ino in inos:
            for a in self.store.ancestors(ino):
                if a.ino in dirs:
                    break
                dirs.add(a.ino)
        self._pending_dirs, self._pending_dirs_gen = dirs, gen
        return dirs

    def _has_uncached(self, ino):
        if self._status_cache_gen != self.gen:
            self._status_cache, self._status_cache_gen = {}, self.gen
        if ino not in self._status_cache:
            self._status_cache[ino] = bool(
                self.store.scalar(
                    f"""WITH RECURSIVE t(i) AS (SELECT ? UNION ALL
                            SELECT n.ino FROM nodes n JOIN t ON n.parent = t.i WHERE n.deleted = 0)
                        SELECT EXISTS(SELECT 1 FROM nodes JOIN t ON nodes.ino = t.i
                            WHERE is_dir = 0 AND cached = 0 AND deleted = 0 AND {NATIVE_SQL})""",
                    (ino,),
                )
            )
        return self._status_cache[ino]

    # =============================================================== actions
    def action(self, n, what):
        """User actions from the file manager menu or the dashboard."""
        if what == "pin":
            with self.store.lock:
                self.store.update(n.ino, pinned=1)
                if n.is_dir:
                    self.store.execute(
                        "UPDATE nodes SET pinned=0 WHERE ino IN (WITH RECURSIVE t(i) AS (SELECT ? UNION ALL "
                        "SELECT ino FROM nodes JOIN t ON parent = t.i) SELECT i FROM t) AND ino<>?",
                        (n.ino, n.ino),
                    )
            self._log("info", self.store.path_of(n.ino), "will be kept available offline")
        elif what in ("unpin", "free"):
            if any(a.pinned for a in self.store.ancestors(n.ino)):
                raise PermissionError(errno.EPERM, "a parent folder is set to stay offline")
            with self.store.lock:
                for x in self.store.subtree(n.ino):
                    if x.pinned:
                        self.store.update(x.ino, pinned=0)
            if what == "free":
                freed = sum(x.size for x in self.store.subtree(n.ino) if self._evict(x))
                self._log("info", self.store.path_of(n.ino), f"freed {freed / 1e6:.1f} MB; files stay available online")
        elif what == "download":
            for x in self.store.subtree(n.ino):
                if not x.is_dir and not x.cached and not x.deleted and not is_native(x.mime):
                    self._download_queue.append(x.ino)
        else:
            raise ValueError(f"unknown action {what!r}")
        self._bump()
        self.sync_now()

    def set_keep_all_offline(self, enabled):
        self.store.set_meta("keep_all_offline", "1" if enabled else "0")
        if enabled:
            self.action(self.store.get(ROOT), "pin")
        else:
            self.store.update(ROOT, pinned=0)
            self._bump()

    def _evict(self, n):
        """Remove a clean cached copy; the file stays available online."""
        if n.is_dir or not n.cached or n.remote_id is None or is_native(n.mime):
            return False
        with self._node_lock(n.ino), self.store.lock:
            cur = self.store.get(n.ino)
            if cur is None or not cur.cached or cur.dirty or self.is_open(n.ino):
                return False
            self._drop_cache(n.ino)
            self.store.update(n.ino, cached=0)
        return True

    # ============================================================ API (UI)
    def start(self):
        self._thread = threading.Thread(target=self._run, name="sync-engine", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def sync_now(self):
        self._force = True
        self._wake.set()

    def request_reset(self):
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

    def restore_deleted(self):
        """Undo pending local deletions (they come back as online-only files)."""
        with self.store.lock:
            tombs = self.store.query("WHERE deleted=1")
            for n in sorted(tombs, key=lambda n: len(self.store.ancestors(n.ino))):
                parent = self.store.get(n.parent)
                if parent is None or parent.deleted:
                    continue
                self.store.update(n.ino, deleted=0, name=self._unique(n.parent, n.name), cached=0, dirty=0)
        with self._lock:
            self.pending_mass_delete = None
        self._log("info", "", f"restored {len(tombs)} deleted item(s) from Drive")

    def status(self):
        # read the database first: never take the store lock while holding self._lock
        cached = self.store.scalar(f"SELECT COALESCE(SUM(size), 0) FROM nodes WHERE cached=1 AND deleted=0 AND {NATIVE_SQL}")
        tracked = self.store.count("WHERE deleted=0 AND ino<>1")
        pending = len(self._pending())
        keep_all = self.store.meta("keep_all_offline") == "1"
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
                "files_tracked": tracked,
                "pending_changes": pending,
                "downloading": len(self.downloading),
                "cached_bytes": cached,
                "cache_limit": int(float(self.config.get("cache_limit_gb")) * 1024**3),
                "keep_all_offline": keep_all,
                "sync_dir": str(self.config.sync_dir),
                "mounted": self.mounted,
            }

    # ============================================================ main loop
    def _run(self):
        next_remote = 0.0
        backoff = 0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if self.mount_error:
                    self._set_state("error", self.mount_error, error=self.mount_error)
                elif self.config.get("paused"):
                    self._set_state("paused", "Sync is paused")
                elif not self.auth.is_connected():
                    self._set_state("disconnected", "Connect your Google account to start syncing")
                else:
                    remote_due = self._force or now >= next_remote
                    local_due = self._force or (
                        self._local_changed and now - self._local_changed >= self.settle_seconds
                    )
                    self._force = False
                    self.sync_once(remote=remote_due, local=local_due or remote_due)
                    if remote_due:
                        next_remote = time.monotonic() + int(self.config.get("poll_interval"))
                    backoff = 0
            except AuthError as e:
                self._set_state("auth_error", str(e), error=str(e))
            except SyncError as e:
                self._set_state("error", str(e), error=str(e))
                next_remote = time.monotonic() + 30
            except (requests.RequestException, DriveError, OSError) as e:
                backoff = min(300, max(15, backoff * 2))
                log.warning("Sync cycle failed, retrying in %ss: %s", backoff, e)
                offline = isinstance(e, (requests.ConnectionError, requests.Timeout))
                msg = "Offline - cached files still work; changes upload when you reconnect" if offline else f"Error: {e}"
                self._set_state("offline" if offline else "error", msg, error=None if offline else str(e))
                next_remote = time.monotonic() + backoff
            except Exception as e:  # never let the worker die
                log.exception("Unexpected sync error")
                self._set_state("error", f"Unexpected error: {e}", error=str(e))
                next_remote = time.monotonic() + 60
            self._wake.wait(1.0)
            self._wake.clear()

    def sync_once(self, remote=True, local=True):
        if self._reset_requested:
            self._reset_requested = False
            self._reset()
        if self.store.meta("initialized") != "1":
            self._initial_listing()
        elif remote:
            self.pull_remote()
        if local:
            self._local_changed = 0.0
            self.push_local()
        self.prefetch()
        self.evict()
        if self.account is None or time.time() - self._account_checked > ACCOUNT_REFRESH:
            self._refresh_account()
        self.last_sync = time.time()
        pending = len(self._pending())
        if self.pending_mass_delete:
            self._set_state("attention", "A large deletion is waiting for your confirmation")
        elif pending or self.downloading:
            self._set_state("syncing", f"{pending} change(s) waiting to upload" if pending else "Downloading…")
        else:
            self._set_state("idle", "Up to date")

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

    def _reset(self):
        """Forget all sync state. Unsynced local work is copied out first."""
        rescued = self._rescue_unsynced()
        self.store.reset_tree()
        for p in self.cache_dir.iterdir():
            p.unlink(missing_ok=True)
        if rescued:
            self._log("info", str(rescued), "unsynced files were copied here before re-syncing")

    def _rescue_unsynced(self):
        nodes = [n for n in self.store.query("WHERE is_dir=0 AND deleted=0 AND cached=1 AND (dirty=1 OR remote_id IS NULL)")
                 if not self.ignored(n.name)]
        if not nodes:
            return None
        dest = Path.home() / f"GDrive unsynced files {datetime.now():%Y-%m-%d %H%M%S}"
        for n in nodes:
            target = dest / self.store.path_of(n.ino).lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.cache_path(n.ino), target)
        return dest

    # ======================================================= Drive → local
    def _initial_listing(self):
        self._set_state("syncing", "Reading the list of files in your Drive…")
        token = self.drive.start_page_token()
        root_id = self.drive.root_id()
        files = self.drive.list_all()
        children = defaultdict(list)
        for f in files:
            if f.get("parents") and f["mimeType"] != SHORTCUT_MIME:
                children[f["parents"][0]].append(f)

        if self._pending():
            self._rescue_unsynced()
        self.store.reset_tree()
        for p in self.cache_dir.iterdir():
            p.unlink(missing_ok=True)
        keep_all = self.store.meta("keep_all_offline") == "1"
        self.store.update(ROOT, remote_id=root_id, pinned=1 if keep_all else 0)
        queue = deque([(root_id, ROOT)])
        while queue:
            parent_rid, parent_ino = queue.popleft()
            taken = set()
            kids = sorted(children.get(parent_rid, []), key=lambda f: (f["mimeType"] != FOLDER_MIME, f["name"], f["id"]))
            with self.store.lock, self.store.conn:
                for f in kids:
                    base = local_name(f["name"], f["mimeType"])
                    name, n = base, 1
                    while name in taken:
                        name, n = numbered(base, n), n + 1
                    taken.add(name)
                    cur = self.store.conn.execute(
                        "INSERT INTO nodes (remote_id, parent, name, is_dir, mime, size, mtime_ns, r_parent, r_name, "
                        "r_md5, r_mtime, link) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        self._remote_row(f, parent_ino, name, parent_rid),
                    )
                    if f["mimeType"] == FOLDER_MIME:
                        queue.append((f["id"], cur.lastrowid))
            self.store._changed(structural=True)
        self.store.set_meta("page_token", token)
        self.store.set_meta("initialized", "1")
        self._log("info", "", f"Drive listed: {self.store.count('WHERE ino<>1'):,} items, nothing downloaded yet")

    @staticmethod
    def _remote_row(f, parent_ino, name, parent_rid):
        mime = f["mimeType"]
        size = len(link_bytes(f["id"], mime, f.get("webViewLink"))) if is_native(mime) else int(f.get("size", 0))
        return (
            f["id"], parent_ino, name, int(mime == FOLDER_MIME), mime, size, time_ns(f.get("modifiedTime")),
            parent_rid, f["name"], f.get("md5Checksum"), f.get("modifiedTime"), f.get("webViewLink"),
        )

    def pull_remote(self):
        changes, new_token = self.drive.changes(self.store.meta("page_token"))
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
                    self._log("error", (c.get("file") or {}).get("name", c["fileId"]), str(e))
                    retry = retry or not isinstance(e, DriveError) or (e.status or 500) >= 500
            if len(deferred) == len(pending):
                break
            pending = deferred
        for c in pending:  # parent is outside My Drive (e.g. moved to a shared drive)
            n = self.store.by_remote(c["fileId"])
            if n is not None:
                self._remote_gone(n)
        if not retry:
            self.store.set_meta("page_token", new_token)

    def _apply_remote(self, c):
        """Apply one Drive change; returns False if its parent folder isn't known yet."""
        fid, f = c["fileId"], c.get("file")
        root = self.store.get(ROOT)
        if fid == root.remote_id:
            return True
        n = self.store.by_remote(fid)
        if c.get("removed") or not f or f.get("trashed") or f["mimeType"] == SHORTCUT_MIME or not f.get("parents"):
            if n is not None:
                self._remote_gone(n)
            return True
        pid = f["parents"][0]
        parent = root if pid == root.remote_id else self.store.by_remote(pid)
        if parent is None or not parent.is_dir or parent.deleted:
            return False
        mime = f["mimeType"]
        with self.store.lock:
            if n is not None and n.is_dir != (mime == FOLDER_MIME):
                self._remote_gone(n)
                n = None
            if n is None:
                name = self._unique(parent.ino, local_name(f["name"], mime))
                row = self._remote_row(f, parent.ino, name, pid)
                cols = ("remote_id", "parent", "name", "is_dir", "mime", "size", "mtime_ns",
                        "r_parent", "r_name", "r_md5", "r_mtime", "link")
                self.store.insert(**dict(zip(cols, row)))
                return True
            if n.deleted:
                return True  # deleted here; the deletion will be applied to Drive
            upd = {}
            if (n.r_parent, n.r_name) != (pid, f["name"]):
                if not n.moved:  # if renamed on both sides, the local name wins
                    want = local_name(f["name"], mime)
                    if (n.parent, remote_name_for(n.name, mime)) != (parent.ino, f["name"]):
                        upd.update(parent=parent.ino, name=self._unique(parent.ino, want, own=n.ino))
                        self._log("move", self.store.path_of(n.ino), "renamed or moved in Drive")
                upd.update(r_parent=pid, r_name=f["name"])
            if is_native(mime):
                upd.update(link=f.get("webViewLink"), r_mtime=f.get("modifiedTime"), mtime_ns=time_ns(f.get("modifiedTime")))
            elif not n.is_dir and self._content_changed(n, f):
                if n.dirty and n.cached:
                    if file_md5(self.cache_path(n.ino)) == f.get("md5Checksum"):
                        upd.update(dirty=0)  # same edit on both sides
                    else:
                        self._conflict_copy(n, "edited both here and in Drive")
                        upd.update(cached=0, dirty=0)
                elif n.cached:
                    self._drop_cache(n.ino)
                    upd.update(cached=0)
                upd.update(
                    size=int(f.get("size", 0)), mtime_ns=time_ns(f.get("modifiedTime")),
                    r_md5=f.get("md5Checksum"), r_mtime=f.get("modifiedTime"),
                )
                if not upd.get("dirty", n.dirty) and "parent" not in upd:
                    self._log("info", self.store.path_of(n.ino), "updated in Drive")
            self.store.update(n.ino, **upd)
        return True

    def _remote_gone(self, n):
        """A file or folder was trashed/removed in Drive. Local unsynced work is kept and re-uploaded."""
        with self.store.lock:
            sub = self.store.subtree(n.ino)
            by_ino = {x.ino: x for x in sub}
            keep = set()
            for x in sub:
                if x.deleted:
                    continue
                if (not x.is_dir and x.dirty and x.cached) or x.remote_id is None:
                    cur = x
                    while cur is not None and cur.ino not in keep:
                        keep.add(cur.ino)
                        if cur.ino == n.ino:
                            break
                        cur = by_ino.get(cur.parent)
            for x in reversed(sub):
                if x.ino in keep:
                    if x.remote_id:
                        self.store.update(x.ino, remote_id=None, r_parent=None, r_name=None, r_md5=None,
                                          r_mtime=None, moved=0)
                else:
                    self._drop_cache(x.ino)
                    self.store.delete(x.ino)
        path = n.name
        if keep:
            self._log("info", path, "removed from Drive but has local changes; uploading it again")
            self._touch_local()
        else:
            self._log("delete", path, "removed from Drive")

    def _conflict_copy(self, n, reason):
        stem, ext = os.path.splitext(n.name)
        stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
        name = self._unique(n.parent, f"{stem} (conflict {self.hostname} {stamp}){ext}")
        with self.store.lock:
            new = self.store.insert(
                parent=n.parent, name=name, is_dir=0, mime=n.mime, size=n.size, mtime_ns=n.mtime_ns,
                cached=1, dirty=1, atime=time.time(),
            )
            if self.cache_path(n.ino).exists():
                os.replace(self.cache_path(n.ino), self.cache_path(new))
            self._retarget(n.ino, new)
        path = self.store.path_of(n.ino).lstrip("/")
        with self._lock:
            self.conflicts.appendleft({"time": time.time(), "path": path, "copy": name, "reason": reason})
        self._log("conflict", path, f"{reason}; your version was saved as '{name}'")
        self._touch_local()
        return new

    # ======================================================= local → Drive
    def push_local(self):
        now = time.monotonic()

        def settling(ino):
            return self.has_writer(ino) or now - self._last_write.get(ino, -1e9) < self.settle_seconds

        # 1. new folders and files, parents first
        progress = True
        while progress:
            progress = False
            for n in self.store.query("WHERE remote_id IS NULL AND deleted=0 AND ino<>1 ORDER BY ino"):
                if self.ignored(n.name) or (not n.is_dir and settling(n.ino)):
                    continue
                parent = self.store.get(n.parent)
                if parent is None or not parent.remote_id:
                    continue
                if self._guard(("create", self.store.path_of(n.ino)), self._push_create, n, parent):
                    progress = True
        # 2. renames and moves
        for n in self.store.query("WHERE moved=1 AND deleted=0 AND remote_id IS NOT NULL"):
            if self.ignored(n.name) and not self.ignored(n.r_name or ""):
                continue  # renamed to a temporary name (e.g. editor backup); wait
            parent = self.store.get(n.parent)
            if parent is not None and parent.remote_id:
                self._guard(("move", self.store.path_of(n.ino)), self._push_move, n, parent)
        # 3. edited content
        for n in self.store.query("WHERE dirty=1 AND cached=1 AND deleted=0 AND is_dir=0 AND remote_id IS NOT NULL"):
            if not settling(n.ino):
                self._guard(("update", self.store.path_of(n.ino)), self._push_content, n)
        # 4. deletions, with a guard against mass deletion
        tombs = self.store.query("WHERE deleted=1")
        if not tombs:
            return
        files = sum(1 for t in tombs if not t.is_dir) or len(tombs)
        threshold = int(self.config.get("mass_delete_threshold"))
        if files > threshold and not self._allow_mass_delete:
            with self._lock:
                self.pending_mass_delete = {"count": files, "examples": [t.name for t in tombs[:10]]}
            log.warning("Holding back deletion of %d items until confirmed in the dashboard", files)
            return
        with self._lock:
            self._allow_mass_delete = False
            self.pending_mass_delete = None
        for t in sorted(tombs, key=lambda t: len(self.store.ancestors(t.ino))):
            if self.store.get(t.ino) is not None:
                self._guard(("delete", t.name), self._push_delete, t)

    def _push_create(self, n, parent):
        remote_name = remote_name_for(n.name, n.mime)
        self._set_busy(self.uploading, n.ino, True)
        try:
            if n.is_dir:
                f = self.drive.create_folder(remote_name, parent.remote_id)
            else:
                f = self.drive.upload_new(self.cache_path(n.ino), remote_name, parent.remote_id, mime=guess_mime(n.name))
        finally:
            self._set_busy(self.uploading, n.ino, False)
        with self.store.lock:
            cur = self.store.get(n.ino)
            if cur is not None:
                upd = dict(remote_id=f["id"], r_parent=parent.remote_id, r_name=f["name"],
                           r_md5=f.get("md5Checksum"), r_mtime=f.get("modifiedTime"))
                if not cur.is_dir:
                    upd["dirty"] = 0 if cur.ver == n.ver else 1
                if (cur.parent, remote_name_for(cur.name, cur.mime)) != (n.parent, remote_name):
                    upd["moved"] = 1
                self.store.update(n.ino, **upd)
        if cur is None:  # deleted locally while uploading
            self.drive.trash(f["id"])
            return
        self._log("up", self.store.path_of(n.ino) + ("/" if n.is_dir else ""))

    def _push_move(self, n, parent):
        remote_name = remote_name_for(n.name, n.mime)
        new_parent = parent.remote_id != n.r_parent
        try:
            f = self.drive.update_metadata(
                n.remote_id,
                name=remote_name if remote_name != n.r_name else None,
                add_parent=parent.remote_id if new_parent else None,
                remove_parent=n.r_parent if new_parent and n.r_parent else None,
            )
        except NotFound:
            self._remote_gone(n)
            return
        with self.store.lock:
            cur = self.store.get(n.ino)
            if cur is not None:
                upd = dict(r_parent=parent.remote_id, r_name=f["name"])
                if (cur.parent, cur.name) == (n.parent, n.name):
                    upd["moved"] = 0
                self.store.update(n.ino, **upd)
        self._log("move", self.store.path_of(n.ino), "renamed/moved in Drive")

    def _push_content(self, n):
        try:
            remote = self.drive.get(n.remote_id)
        except NotFound:
            remote = None
        if remote is None or remote.get("trashed"):
            self._remote_gone(n)
            return
        if self._content_changed(n, remote):
            with self.store.lock:
                if file_md5(self.cache_path(n.ino)) == remote.get("md5Checksum"):
                    self.store.update(n.ino, dirty=0, r_md5=remote.get("md5Checksum"), r_mtime=remote.get("modifiedTime"))
                    return
                self._conflict_copy(n, "edited both here and in Drive")
                self.store.update(
                    n.ino, cached=0, dirty=0, size=int(remote.get("size", 0)),
                    mtime_ns=time_ns(remote.get("modifiedTime")),
                    r_md5=remote.get("md5Checksum"), r_mtime=remote.get("modifiedTime"),
                )
            return
        self._set_busy(self.uploading, n.ino, True)
        try:
            f = self.drive.upload_update(n.remote_id, self.cache_path(n.ino), mime=guess_mime(n.name))
        finally:
            self._set_busy(self.uploading, n.ino, False)
        with self.store.lock:
            cur = self.store.get(n.ino)
            if cur is not None:
                self.store.update(n.ino, r_md5=f.get("md5Checksum"), r_mtime=f.get("modifiedTime"),
                                  dirty=0 if cur.ver == n.ver else 1)
        self._log("up", self.store.path_of(n.ino), "updated")

    def _push_delete(self, t):
        try:
            self.drive.trash(t.remote_id)
        except NotFound:
            pass
        with self.store.lock:
            for x in self.store.subtree(t.ino):
                if x.deleted:
                    self._drop_cache(x.ino)
                    self.store.delete(x.ino)
        self._log("delete", t.name, "deleted here; moved to Drive trash")

    # ================================================== offline & cache size
    def _pinned_inos(self):
        with self.store.lock:
            rows = self.store.conn.execute(
                """WITH RECURSIVE p(i) AS (SELECT ino FROM nodes WHERE pinned=1 AND deleted=0 UNION
                       SELECT n.ino FROM nodes n JOIN p ON n.parent = p.i WHERE n.deleted=0) SELECT i FROM p"""
            )
            return {r[0] for r in rows}

    def prefetch(self):
        """Download pinned files and explicit 'download now' requests."""
        deadline = time.monotonic() + self.prefetch_budget
        with self.store.lock:
            rows = self.store.conn.execute(
                f"""WITH RECURSIVE p(i) AS (SELECT ino FROM nodes WHERE pinned=1 AND deleted=0 UNION
                        SELECT n.ino FROM nodes n JOIN p ON n.parent = p.i WHERE n.deleted=0)
                    SELECT ino FROM nodes JOIN p ON nodes.ino = p.i
                    WHERE is_dir=0 AND cached=0 AND remote_id IS NOT NULL AND {NATIVE_SQL} LIMIT 500"""
            ).fetchall()
        todo = [r[0] for r in rows]
        while self._download_queue:
            todo.append(self._download_queue.popleft())
        for ino in todo:
            if time.monotonic() > deadline:
                self._wake.set()  # more to do: come back right away
                break
            self._guard(("download", self.store.path_of(ino)), self.hydrate, ino)

    def evict(self):
        """Keep recently opened (not pinned) files within the cache size limit, oldest first."""
        limit = int(float(self.config.get("cache_limit_gb")) * 1024**3)
        pinned = self._pinned_inos()
        cached = [n for n in self.store.query(f"WHERE cached=1 AND deleted=0 AND is_dir=0 AND {NATIVE_SQL} ORDER BY atime")
                  if n.ino not in pinned]
        used = sum(n.size for n in cached)
        if used <= limit:
            return
        for n in cached:
            if used <= limit * 0.9:
                break
            if not n.dirty and n.remote_id and self._evict(n):
                used -= n.size
