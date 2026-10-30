"""SQLite metadata for the virtual Drive folder.

Every file and folder is a *node* with a stable local inode number. A node
records both what the user sees (parent, name, size) and the last state
synced with Drive (``r_*`` columns), so the differences between the two are
exactly the work left to do:

* ``remote_id IS NULL``  - created locally, not yet in Drive
* ``dirty = 1``          - content edited locally, not yet uploaded
* ``moved = 1``          - renamed/moved locally, not yet applied in Drive
* ``deleted = 1``        - deleted locally (tombstone), not yet trashed in Drive
"""

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, fields

ROOT = 1


@dataclass
class Node:
    ino: int
    remote_id: str | None
    parent: int | None
    name: str
    is_dir: bool
    mime: str | None
    size: int
    mtime_ns: int
    r_parent: str | None
    r_name: str | None
    r_md5: str | None
    r_mtime: str | None
    link: str | None
    cached: bool
    dirty: bool
    pinned: bool
    deleted: bool
    moved: bool
    ver: int
    atime: float


COLUMNS = [f.name for f in fields(Node)]
_BOOLS = ("is_dir", "cached", "dirty", "pinned", "deleted", "moved")
_SELECT = f"SELECT {','.join(COLUMNS)} FROM nodes"

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    ino INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_id TEXT UNIQUE,
    parent INTEGER,
    name TEXT NOT NULL,
    is_dir INTEGER NOT NULL DEFAULT 0,
    mime TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    r_parent TEXT,
    r_name TEXT,
    r_md5 TEXT,
    r_mtime TEXT,
    link TEXT,
    cached INTEGER NOT NULL DEFAULT 0,
    dirty INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    moved INTEGER NOT NULL DEFAULT 0,
    ver INTEGER NOT NULL DEFAULT 0,
    atime REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS nodes_live_name ON nodes(parent, name) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS nodes_parent ON nodes(parent);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path, on_change=None):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.on_change = on_change
        self._paths = {}
        with self.lock, self.conn:
            if self.get(ROOT) is None:
                self.conn.execute("INSERT INTO nodes (ino, parent, name, is_dir) VALUES (?, NULL, '', 1)", (ROOT,))

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _node(row):
        if row is None:
            return None
        n = Node(*row)
        for b in _BOOLS:
            setattr(n, b, bool(getattr(n, b)))
        return n

    def _changed(self, structural=False):
        if structural:
            self._paths.clear()
        if self.on_change:
            self.on_change()

    @contextmanager
    def transaction(self):
        with self.lock, self.conn:
            yield

    def query(self, where="", params=()):
        with self.lock:
            return [self._node(r) for r in self.conn.execute(f"{_SELECT} {where}", params)]

    # -- lookups ------------------------------------------------------------
    def get(self, ino):
        with self.lock:
            return self._node(self.conn.execute(f"{_SELECT} WHERE ino=?", (ino,)).fetchone())

    def by_remote(self, remote_id):
        with self.lock:
            return self._node(self.conn.execute(f"{_SELECT} WHERE remote_id=?", (remote_id,)).fetchone())

    def child(self, parent, name):
        with self.lock:
            return self._node(
                self.conn.execute(f"{_SELECT} WHERE parent=? AND name=? AND deleted=0", (parent, name)).fetchone()
            )

    def children(self, parent):
        return self.query("WHERE parent=? AND deleted=0 ORDER BY name", (parent,))

    def resolve(self, path):
        """Map an absolute path inside the mount ('/a/b') to a live node."""
        with self.lock:
            ino = self._paths.get(path)
            if ino is not None:
                n = self.get(ino)
                if n is not None and not n.deleted:
                    return n
            node = self.get(ROOT)
            for part in path.strip("/").split("/"):
                if not part:
                    continue
                if not node.is_dir:
                    return None
                node = self.child(node.ino, part)
                if node is None:
                    return None
            if len(self._paths) > 50_000:
                self._paths.clear()
            self._paths[path] = node.ino
            return node

    def path_of(self, ino):
        parts = []
        with self.lock:
            n = self.get(ino)
            while n is not None and n.ino != ROOT:
                parts.append(n.name)
                n = self.get(n.parent)
        return "/" + "/".join(reversed(parts))

    def ancestors(self, ino):
        """Nodes from ``ino``'s parent up to the root."""
        out = []
        with self.lock:
            n = self.get(ino)
            while n is not None and n.parent is not None:
                n = self.get(n.parent)
                if n is not None:
                    out.append(n)
        return out

    def subtree(self, ino):
        """``ino`` and all its descendants, live or deleted, parents first."""
        with self.lock:
            rows = self.conn.execute(
                f"""WITH RECURSIVE t(i, d) AS (SELECT ?, 0 UNION ALL
                        SELECT n.ino, t.d + 1 FROM nodes n JOIN t ON n.parent = t.i)
                    {_SELECT.replace('FROM nodes', 'FROM nodes JOIN t ON nodes.ino = t.i')} ORDER BY t.d""",
                (ino,),
            )
            return [self._node(r) for r in rows]

    def count(self, where="", params=()):
        with self.lock:
            return self.conn.execute(f"SELECT COUNT(*) FROM nodes {where}", params).fetchone()[0]

    def scalar(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()[0]

    # -- writes -------------------------------------------------------------
    def insert(self, **values):
        cols = list(values)
        with self.lock, self.conn:
            cur = self.conn.execute(
                f"INSERT INTO nodes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [values[c] for c in cols],
            )
        self._changed(structural=True)
        return cur.lastrowid

    def update(self, ino, **values):
        if not values:
            return
        cols = list(values)
        with self.lock, self.conn:
            self.conn.execute(
                f"UPDATE nodes SET {','.join(f'{c}=?' for c in cols)} WHERE ino=?",
                [values[c] for c in cols] + [ino],
            )
        self._changed(structural=bool({"parent", "name", "deleted"} & set(cols)))

    def execute(self, sql, params=()):
        with self.lock, self.conn:
            self.conn.execute(sql, params)
        self._changed(structural=True)

    def delete(self, ino):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM nodes WHERE ino=?", (ino,))
        self._changed(structural=True)

    def reset_tree(self):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM nodes WHERE ino<>?", (ROOT,))
            self.conn.execute("UPDATE nodes SET remote_id=NULL, pinned=0 WHERE ino=?", (ROOT,))
            self.conn.execute("DELETE FROM meta WHERE key NOT IN ('keep_all_offline')")
        self._changed(structural=True)

    def meta(self, key, default=None):
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_meta(self, key, value):
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
