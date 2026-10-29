"""SQLite store of the last state on which local disk and Drive agreed."""

import sqlite3
import threading
from dataclasses import dataclass, fields


@dataclass
class Entry:
    id: str
    path: str            # relative POSIX path inside the sync folder
    parent_id: str
    name: str            # remote (Drive) name
    is_dir: bool
    mime: str
    md5: str | None
    remote_mtime: str | None
    local_mtime_ns: int
    local_size: int
    inode: int


COLUMNS = [f.name for f in fields(Entry)]

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    parent_id TEXT,
    name TEXT NOT NULL,
    is_dir INTEGER NOT NULL,
    mime TEXT,
    md5 TEXT,
    remote_mtime TEXT,
    local_mtime_ns INTEGER,
    local_size INTEGER,
    inode INTEGER
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _subtree_clause():
    # avoids LIKE so names containing % or _ are handled correctly
    return "(path = ? OR substr(path, 1, length(?) + 1) = ? || '/')"


class StateDB:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    def _row(self, row):
        if row is None:
            return None
        e = Entry(*row)
        e.is_dir = bool(e.is_dir)
        return e

    def get(self, file_id):
        with self.lock:
            return self._row(self.conn.execute(f"SELECT {','.join(COLUMNS)} FROM entries WHERE id=?", (file_id,)).fetchone())

    def by_path(self, path):
        with self.lock:
            return self._row(self.conn.execute(f"SELECT {','.join(COLUMNS)} FROM entries WHERE path=?", (path,)).fetchone())

    def all(self):
        with self.lock:
            return [self._row(r) for r in self.conn.execute(f"SELECT {','.join(COLUMNS)} FROM entries")]

    def subtree(self, path):
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {','.join(COLUMNS)} FROM entries WHERE {_subtree_clause()}", (path, path, path)
            )
            return [self._row(r) for r in rows]

    def count(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def upsert(self, e):
        with self.lock, self.conn:
            # a different id may still hold this path (e.g. replaced remotely)
            self.conn.execute("DELETE FROM entries WHERE path=? AND id<>?", (e.path, e.id))
            self.conn.execute(
                f"INSERT OR REPLACE INTO entries ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                tuple(getattr(e, c) for c in COLUMNS),
            )

    def delete(self, file_id):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM entries WHERE id=?", (file_id,))

    def delete_subtree(self, path):
        with self.lock, self.conn:
            self.conn.execute(f"DELETE FROM entries WHERE {_subtree_clause()}", (path, path, path))

    def rename_subtree(self, old, new):
        with self.lock, self.conn:
            self.conn.execute(
                f"UPDATE entries SET path = ? || substr(path, ?) WHERE {_subtree_clause()}",
                (new, len(old) + 1, old, old, old),
            )

    def clear(self):
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM entries")
            self.conn.execute("DELETE FROM meta")

    def meta(self, key, default=None):
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_meta(self, key, value):
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
