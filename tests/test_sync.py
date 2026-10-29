"""Sync engine tests against an in-memory fake of the Drive API.

Run: python -m unittest discover -s tests
"""

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_tmp_home = tempfile.mkdtemp()
os.environ["XDG_CONFIG_HOME"] = os.path.join(_tmp_home, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_tmp_home, "data")

from gdrive_sync.config import DEFAULTS  # noqa: E402
from gdrive_sync.db import StateDB  # noqa: E402
from gdrive_sync.drive import FOLDER_MIME, SHORTCUT_MIME, NotFound  # noqa: E402
from gdrive_sync.sync import SyncEngine, SyncError  # noqa: E402

DOC_MIME = "application/vnd.google-apps.document"


class FakeDrive:
    def __init__(self):
        self.root = "root"
        self.files = {}
        self.log = []
        self.n = 0
        self.clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.downloads = 0
        self.uploads = 0

    # ---- simulate another device editing Drive
    def _touch(self, fid):
        self.clock += timedelta(seconds=1)
        self.files[fid]["modifiedTime"] = self.clock.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.log.append(fid)

    def r_create(self, name, parent=None, content=b"", mime="text/plain"):
        self.n += 1
        fid = f"id{self.n}"
        f = {"id": fid, "name": name, "mimeType": mime, "parents": [parent or self.root], "trashed": False}
        if mime not in (FOLDER_MIME, SHORTCUT_MIME) and not mime.startswith("application/vnd.google-apps."):
            f["content"] = content
            f["md5Checksum"] = hashlib.md5(content).hexdigest()
            f["size"] = str(len(content))
        if mime.startswith("application/vnd.google-apps.") and mime != FOLDER_MIME:
            f["webViewLink"] = f"https://docs.google.com/document/d/{fid}/edit"
        self.files[fid] = f
        self._touch(fid)
        return fid

    def r_folder(self, name, parent=None):
        return self.r_create(name, parent, mime=FOLDER_MIME)

    def r_modify(self, fid, content):
        f = self.files[fid]
        f.update(content=content, md5Checksum=hashlib.md5(content).hexdigest(), size=str(len(content)))
        self._touch(fid)

    def r_rename(self, fid, name):
        self.files[fid]["name"] = name
        self._touch(fid)

    def r_move(self, fid, parent):
        self.files[fid]["parents"] = [parent]
        self._touch(fid)

    def r_trash(self, fid):
        self.files[fid]["trashed"] = True
        self._touch(fid)

    def child(self, name, parent=None):
        parent = parent or self.root
        hits = [f for f in self.files.values() if f["name"] == name and f["parents"] == [parent] and not f["trashed"]]
        assert len(hits) <= 1, hits
        return hits[0] if hits else None

    # ---- API used by the engine
    def _meta(self, fid):
        f = dict(self.files[fid])
        f.pop("content", None)
        f["parents"] = list(f["parents"])
        return f

    def about(self):
        return {"user": {"emailAddress": "me@example.com"}, "storageQuota": {"usage": "1", "limit": "100"}}

    def root_id(self):
        return self.root

    def get(self, fid):
        if fid not in self.files:
            raise NotFound("nope", 404)
        return self._meta(fid)

    def list_all(self):
        return [self._meta(i) for i, f in self.files.items() if not f["trashed"]]

    def start_page_token(self):
        return str(len(self.log))

    def changes(self, token):
        ids = self.log[int(token):]
        return [{"fileId": i, "removed": False, "file": self._meta(i)} for i in ids], str(len(self.log))

    def create_folder(self, name, parent_id):
        return self._meta(self.r_folder(name, parent_id))

    def update_metadata(self, fid, name=None, add_parent=None, remove_parent=None):
        if fid not in self.files:
            raise NotFound("nope", 404)
        if name:
            self.files[fid]["name"] = name
        if add_parent:
            self.files[fid]["parents"] = [add_parent]
        self._touch(fid)
        return self._meta(fid)

    def trash(self, fid):
        self.r_trash(fid)

    def upload_new(self, path, name, parent_id):
        self.uploads += 1
        return self._meta(self.r_create(name, parent_id, Path(path).read_bytes()))

    def upload_update(self, fid, path):
        self.uploads += 1
        self.r_modify(fid, Path(path).read_bytes())
        return self._meta(fid)

    def download(self, fid, dest, expected_md5=None):
        self.downloads += 1
        data = self.files[fid]["content"]
        Path(dest).write_bytes(data)
        return hashlib.md5(data).hexdigest()


class FakeConfig:
    def __init__(self, sync_dir, **overrides):
        self.data = dict(DEFAULTS, sync_dir=str(sync_dir), **overrides)

    def get(self, key):
        return self.data.get(key)

    def update(self, **kw):
        self.data.update(kw)

    @property
    def sync_dir(self):
        return Path(self.data["sync_dir"])


class FakeAuth:
    def is_connected(self):
        return True


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "GoogleDrive"
        self.trash_dir = self.tmp / "Trash"
        self.trash_dir.mkdir()
        self.drive = FakeDrive()
        self.config = FakeConfig(self.root)
        self.engine = SyncEngine(self.config, FakeAuth(), self.drive, StateDB(":memory:"), trash=self._trash)
        self.engine.settle_seconds = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _trash(self, path):
        shutil.move(str(path), str(self.trash_dir / f"{len(list(self.trash_dir.iterdir()))}-{Path(path).name}"))

    def sync(self):
        self.engine.sync_once()

    def write(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def read(self, rel):
        return (self.root / rel).read_bytes()

    # ------------------------------------------------------------------
    def test_initial_sync_mirrors_drive_and_uploads_local_files(self):
        d = self.drive
        docs = d.r_folder("Docs")
        d.r_create("a.txt", docs, b"alpha")
        d.r_create("b.txt", content=b"bravo")
        d.r_create("Notes", mime=DOC_MIME)
        d.r_create("link", mime=SHORTCUT_MIME)
        self.write("local.txt", b"from laptop")
        self.sync()

        self.assertEqual(self.read("Docs/a.txt"), b"alpha")
        self.assertEqual(self.read("b.txt"), b"bravo")
        self.assertEqual(json.loads(self.read("Notes.gdoc"))["doc_id"], d.child("Notes")["id"])
        self.assertFalse((self.root / "link").exists())
        self.assertEqual(d.child("local.txt")["content"], b"from laptop")
        self.assertEqual(oct(self.root.stat().st_mode & 0o777), "0o700")

    def test_local_changes_reach_drive(self):
        d = self.drive
        docs = d.r_folder("Docs")
        a_id = d.r_create("a.txt", docs, b"alpha")
        b_id = d.r_create("b.txt", content=b"bravo")
        self.sync()

        self.write("New/x.txt", b"x")
        self.write("b.txt", b"bravo v2")
        self.sync()
        new = d.child("New")
        self.assertEqual(d.child("x.txt", new["id"])["content"], b"x")
        self.assertEqual(d.files[b_id]["content"], b"bravo v2")

        os.rename(self.root / "b.txt", self.root / "c.txt")          # rename file
        os.rename(self.root / "Docs", self.root / "New" / "Docs2")   # move + rename folder
        (self.root / "New" / "x.txt").unlink()                      # delete
        self.sync()
        self.assertEqual(d.files[b_id]["name"], "c.txt")
        self.assertEqual(d.files[docs]["name"], "Docs2")
        self.assertEqual(d.files[docs]["parents"], [new["id"]])
        self.assertEqual(d.files[a_id]["parents"], [docs])  # child kept its id, nothing re-uploaded
        self.assertTrue(d.child("x.txt", new["id"]) is None)
        self.assertEqual(d.uploads, 2)

    def test_drive_changes_reach_disk(self):
        d = self.drive
        f_id = d.r_create("f.txt", content=b"one")
        gone = d.r_create("gone.txt", content=b"bye")
        self.sync()

        folder = d.r_folder("Photos")
        d.r_create("p.jpg", folder, b"jpeg")
        d.r_modify(f_id, b"two")
        d.r_rename(f_id, "renamed.txt")
        d.r_trash(gone)
        self.sync()
        self.assertEqual(self.read("Photos/p.jpg"), b"jpeg")
        self.assertEqual(self.read("renamed.txt"), b"two")
        self.assertFalse((self.root / "f.txt").exists())
        self.assertFalse((self.root / "gone.txt").exists())
        self.assertEqual(len(list(self.trash_dir.iterdir())), 1)  # moved to Trash, not deleted

        d.r_move(f_id, folder)
        self.sync()
        self.assertEqual(self.read("Photos/renamed.txt"), b"two")

    def test_own_uploads_are_not_downloaded_again(self):
        self.sync()
        self.write("mine.txt", b"data")
        self.sync()
        self.sync()
        self.assertEqual(self.drive.downloads, 0)
        self.assertEqual(self.drive.uploads, 1)

    def test_edit_on_both_sides_keeps_both_versions(self):
        d = self.drive
        f_id = d.r_create("report.txt", content=b"original")
        self.sync()
        self.write("report.txt", b"laptop edit")
        d.r_modify(f_id, b"phone edit")
        self.sync()

        self.assertEqual(self.read("report.txt"), b"phone edit")
        copies = [p for p in self.root.iterdir() if p.name.startswith("report (conflict")]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), b"laptop edit")
        self.assertEqual(d.child(copies[0].name)["content"], b"laptop edit")
        self.assertEqual(len(self.engine.conflicts), 1)

    def test_local_edit_wins_over_remote_delete(self):
        d = self.drive
        f_id = d.r_create("keep.txt", content=b"v1")
        self.sync()
        self.write("keep.txt", b"v2 local")
        d.r_trash(f_id)
        self.sync()
        self.assertEqual(self.read("keep.txt"), b"v2 local")
        self.assertEqual(d.child("keep.txt")["content"], b"v2 local")

    def test_mass_delete_requires_confirmation(self):
        self.config.update(mass_delete_threshold=3)
        for i in range(5):
            self.drive.r_create(f"f{i}.txt", content=b"x")
        self.sync()
        for p in list(self.root.iterdir()):
            p.unlink()
        self.sync()
        self.assertEqual(len(self.drive.list_all()), 5)
        self.assertEqual(self.engine.pending_mass_delete["count"], 5)

        self.engine.confirm_mass_delete()
        self.sync()
        self.assertEqual(len(self.drive.list_all()), 0)
        self.assertIsNone(self.engine.pending_mass_delete)

    def test_missing_sync_folder_halts_instead_of_deleting(self):
        self.drive.r_create("a.txt", content=b"a")
        self.sync()
        shutil.rmtree(self.root)
        with self.assertRaises(SyncError):
            self.sync()
        self.assertEqual(len(self.drive.list_all()), 1)

    def test_duplicate_and_unsafe_names_stay_inside_folder(self):
        d = self.drive
        d.r_create("dup.txt", content=b"1")
        d.r_create("dup.txt", content=b"2")
        d.r_create("..", content=b"dots")
        d.r_create("../../escape.txt", content=b"evil")
        self.sync()
        names = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(names, [".._.._escape.txt", "_..", "dup (1).txt", "dup.txt"])
        self.assertFalse((self.tmp / "escape.txt").exists())
        self.sync()  # stable: nothing renamed or re-uploaded
        self.assertEqual(d.uploads, 0)
        self.assertEqual(len(d.list_all()), 4)

    def test_symlinks_are_never_uploaded(self):
        self.sync()
        secret = self.tmp / "secret.txt"
        secret.write_text("private")
        os.symlink(secret, self.root / "link.txt")
        os.symlink(self.tmp, self.root / "linkdir")
        self.sync()
        self.assertEqual(self.drive.uploads, 0)

    def test_ignored_temp_files_are_skipped(self):
        self.sync()
        self.write("doc.txt.swp", b"vim")
        self.write(".~lock.doc.odt#", b"lo")
        self.sync()
        self.assertEqual(self.drive.uploads, 0)

    def test_folder_deleted_in_drive_with_local_additions_is_kept(self):
        d = self.drive
        folder = d.r_folder("Shared")
        d.r_create("old.txt", folder, b"old")
        self.sync()
        self.write("Shared/new.txt", b"new local file")
        d.r_trash(folder)
        self.engine._remote_pass()  # remote first: the new local file is not uploaded yet
        self.assertTrue((self.root / "Shared" / "new.txt").exists())
        self.sync()
        again = d.child("Shared")
        self.assertIsNotNone(again)
        self.assertEqual(d.child("new.txt", again["id"])["content"], b"new local file")


if __name__ == "__main__":
    unittest.main()
