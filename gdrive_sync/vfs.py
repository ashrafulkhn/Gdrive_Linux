"""FUSE filesystem presenting Google Drive as a normal folder.

Listings and file sizes come from the metadata store, so browsing never
downloads anything. Content is fetched when a program actually opens a file
(except thumbnailers and search indexers, which would otherwise download
your whole Drive just to draw icons).

Status for the file manager integration is exposed as extended attributes:
    user.gdrive.status      cloud | local | pinned | syncing | error
    user.gdrive.link        https link to the file in Google Drive
    user.gdrive.generation  (root only) changes whenever any status changes
    user.gdrive.action      write-only: pin | unpin | free | download
"""

import errno
import logging
import os
import stat
import time
from pathlib import Path

from mfusepy import FuseOSError, Operations, fuse_get_context

from .engine import HEAD_BYTES, is_native, link_bytes
from .store import ROOT

log = logging.getLogger(__name__)

XATTR_STATUS = "user.gdrive.status"
XATTR_LINK = "user.gdrive.link"
XATTR_GEN = "user.gdrive.generation"
XATTR_ACTION = "user.gdrive.action"
ENOATTR = getattr(errno, "ENOATTR", errno.ENODATA)

# Processes that only peek at files; they must not trigger downloads.
BACKGROUND_READERS = ("tracker", "localsearch", "baloo", "updatedb", "mlocate", "plocate")


def caller_comm():
    try:
        pid = fuse_get_context()[2]
        return Path(f"/proc/{pid}/comm").read_text().strip().lower()
    except Exception:
        return ""


def errno_of(e):
    return e.errno if isinstance(e, OSError) and e.errno else errno.EIO


class DriveFS(Operations):
    use_ns = True

    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store
        self.uid, self.gid = os.getuid(), os.getgid()
        self.caller = caller_comm  # replaceable in tests
        self.started_ns = time.time_ns()

    # ------------------------------------------------------------ helpers
    def _node(self, path):
        n = self.store.resolve(path)
        if n is None:
            raise FuseOSError(errno.ENOENT)
        return n

    def _parent_and_name(self, path):
        parent_path, name = os.path.split(path.rstrip("/"))
        parent = self._node(parent_path or "/")
        if not parent.is_dir:
            raise FuseOSError(errno.ENOTDIR)
        if not name or name in (".", "..") or len(name.encode()) > 255:
            raise FuseOSError(errno.EINVAL)
        return parent, name

    def _writable_tree(self):
        if not self.store.get(ROOT).remote_id:
            raise FuseOSError(errno.EROFS)  # not connected to Drive yet

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except FuseOSError:
            raise
        except OSError as e:
            raise FuseOSError(errno_of(e)) from e

    def _background_reader(self):
        comm = self.caller()
        return "thum" in comm or comm.startswith(BACKGROUND_READERS)

    def init(self, path):
        self.engine.mounted = True  # the kernel accepted the mount

    def destroy(self, path):
        self.engine.mounted = False

    # ------------------------------------------------------------ metadata
    def getattr(self, path, fh=None):
        n = self._node(path)
        if n.is_dir:
            mode, size, nlink = stat.S_IFDIR | 0o700, 4096, 2
        else:
            mode = stat.S_IFREG | (0o400 if is_native(n.mime) else 0o600)
            size, nlink = n.size, 1
        mtime = n.mtime_ns or self.started_ns
        fd = None if n.is_dir else self.engine.writer_fd(n.ino)
        if fd is not None:
            st = os.fstat(fd)
            size, mtime = st.st_size, st.st_mtime_ns
        return {
            "st_mode": mode,
            "st_nlink": nlink,
            "st_size": size,
            "st_uid": self.uid,
            "st_gid": self.gid,
            "st_mtime": mtime,
            "st_ctime": mtime,
            "st_atime": mtime,
            "st_blksize": 4096,
            # cloud-only files take no space on this computer
            "st_blocks": (size + 511) // 512 if (n.cached or n.is_dir) else 0,
        }

    def readdir(self, path, fh):
        n = self._node(path)
        if not n.is_dir:
            raise FuseOSError(errno.ENOTDIR)
        return [".", ".."] + [c.name for c in self.store.children(n.ino)]

    def statfs(self, path):
        acct = self.engine.account or {}
        limit = acct.get("limit") or 1 << 50
        free = max(0, limit - acct.get("usage", 0))
        bs = 4096
        return {"f_bsize": bs, "f_frsize": bs, "f_blocks": limit // bs, "f_bfree": free // bs,
                "f_bavail": free // bs, "f_files": 1 << 24, "f_ffree": 1 << 23, "f_namemax": 255}

    def utimens(self, path, times=None):
        n = self._node(path)
        mtime = times[1] if times else None
        self.engine.set_mtime(n, mtime if mtime is not None else time.time_ns())
        return 0

    def chmod(self, path, mode):
        self._node(path)
        return 0  # permissions are fixed (private to you); accept so cp -p / rsync work

    def chown(self, path, uid, gid):
        self._node(path)
        return 0

    # --------------------------------------------------------- tree changes
    def mkdir(self, path, mode):
        self._writable_tree()
        parent, name = self._parent_and_name(path)
        self._call(self.engine.make_dir, parent.ino, name)
        return 0

    def rmdir(self, path):
        n = self._node(path)
        if n.ino == ROOT:
            raise FuseOSError(errno.EBUSY)
        if not n.is_dir:
            raise FuseOSError(errno.ENOTDIR)
        if self.store.children(n.ino):
            raise FuseOSError(errno.ENOTEMPTY)
        self.engine.remove(n)
        return 0

    def unlink(self, path):
        n = self._node(path)
        if n.is_dir:
            raise FuseOSError(errno.EISDIR)
        self.engine.remove(n)
        return 0

    def rename(self, old, new):
        self._writable_tree()
        src = self._node(old)
        if src.ino == ROOT:
            raise FuseOSError(errno.EBUSY)
        parent, name = self._parent_and_name(new)
        self._call(self.engine.rename, src, parent, name)
        return 0

    # ----------------------------------------------------------- file data
    def create(self, path, mode, fi=None):
        self._writable_tree()
        parent, name = self._parent_and_name(path)
        ino, fd = self._call(self.engine.create_file, parent.ino, name)
        return self.engine.open_handle(ino, writable=True, fd=fd)

    def open(self, path, flags):
        n = self._node(path)
        if n.is_dir:
            raise FuseOSError(errno.EISDIR)
        writable = (flags & os.O_ACCMODE) != os.O_RDONLY
        if is_native(n.mime):
            if writable:
                raise FuseOSError(errno.EACCES)
            return self.engine.open_handle(n.ino, False, virtual=link_bytes(n.remote_id, n.mime, n.link))
        if writable:
            self._writable_tree()
        if not n.cached and self._background_reader():
            raise FuseOSError(errno.EACCES)
        if writable and flags & os.O_TRUNC:
            self._call(self.engine.truncate, n, 0)
        elif writable:
            self._call(self.engine.hydrate, n.ino)
        fh = self.engine.open_handle(n.ino, writable)
        if writable:
            self._call(self.engine.ensure_fd, self.engine.handle(fh))
        return fh

    def read(self, path, size, offset, fh):
        h = self._call(self.engine.handle, fh)
        if h.virtual is not None:
            return h.virtual[offset : offset + size]
        if h.fd is None:
            n = self.store.get(h.ino)
            if n is None:
                raise FuseOSError(errno.ENOENT)
            # file-type sniffing reads only the first few KB: don't download everything for that
            if not n.cached and n.remote_id and offset + size <= HEAD_BYTES < n.size:
                return self._call(self.engine.read_head, n, offset, size)
        fd = self._call(self.engine.ensure_fd, h)
        return os.pread(fd, size, offset)

    def write(self, path, data, offset, fh):
        h = self._call(self.engine.handle, fh)
        if not h.writable:
            raise FuseOSError(errno.EBADF)
        fd = self._call(self.engine.ensure_fd, h)
        written = os.pwrite(fd, data, offset)
        self.engine.note_write(h)
        return written

    def truncate(self, path, length, fh=None):
        n = self._node(path) if path else self.store.get(self.engine.handle(fh).ino)
        self._call(self.engine.truncate, n, length, fh)
        return 0

    def flush(self, path, fh):
        return 0

    def fsync(self, path, datasync, fh):
        h = self._call(self.engine.handle, fh)
        if h.fd is not None:
            os.fsync(h.fd)
        return 0

    def release(self, path, fh):
        self.engine.close_handle(fh)
        return 0

    # ------------------------------------------------------ status xattrs
    def listxattr(self, path):
        n = self._node(path)
        names = [XATTR_STATUS, XATTR_LINK]
        if n.ino == ROOT:
            names.append(XATTR_GEN)
        return names

    def getxattr(self, path, name, position=0):
        n = self._node(path)
        if name == XATTR_STATUS:
            return self.engine.status_of(n).encode()
        if name == XATTR_GEN and n.ino == ROOT:
            return str(self.engine.gen).encode()
        if name == XATTR_LINK and n.remote_id:
            if n.link:
                return n.link.encode()
            kind = "drive/folders" if n.is_dir else "file/d"
            return f"https://drive.google.com/{kind}/{n.remote_id}".encode()
        raise FuseOSError(ENOATTR)

    def setxattr(self, path, name, value, options, position=0):
        n = self._node(path)
        if name != XATTR_ACTION:
            raise FuseOSError(errno.ENOTSUP)
        try:
            self.engine.action(n, value.decode().strip())
        except ValueError:
            raise FuseOSError(errno.EINVAL)
        except PermissionError:
            raise FuseOSError(errno.EPERM)
        return 0

    def removexattr(self, path, name):
        raise FuseOSError(errno.ENOTSUP)
