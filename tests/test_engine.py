"""On-demand engine + filesystem operations, driven directly (no mount needed).

Run: python -m unittest discover -s tests
"""

import errno
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

_tmp_home = tempfile.mkdtemp()
os.environ.setdefault("XDG_CONFIG_HOME", os.path.join(_tmp_home, "config"))
os.environ.setdefault("XDG_DATA_HOME", os.path.join(_tmp_home, "data"))

from mfusepy import FuseOSError  # noqa: E402

from fakes import DOC_MIME, FakeAuth, FakeConfig, FakeDrive  # noqa: E402
from gdrive_sync.drive import SHORTCUT_MIME  # noqa: E402
from gdrive_sync.engine import Engine  # noqa: E402
from gdrive_sync.store import Store  # noqa: E402
from gdrive_sync.vfs import XATTR_ACTION, XATTR_STATUS, DriveFS  # noqa: E402

O_RW, O_RD = os.O_RDWR, os.O_RDONLY


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.drive = FakeDrive()
        self.config = FakeConfig(self.tmp / "GoogleDrive")
        self.store = Store(":memory:")
        self.engine = Engine(self.config, FakeAuth(), self.drive, self.store, self.tmp / "cache")
        self.engine.settle_seconds = 0
        self.fs = DriveFS(self.engine)
        self.fs.caller = lambda: "python3"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers acting like a program using the folder
    def sync(self):
        self.engine.sync_once()

    def write(self, path, data):
        try:
            self.fs.getattr(path)
            fh = self.fs.open(path, os.O_WRONLY | os.O_TRUNC)
        except FuseOSError:
            fh = self.fs.create(path, 0o644)
        self.fs.write(path, data, 0, fh)
        self.fs.release(path, fh)

    def read(self, path):
        fh = self.fs.open(path, O_RD)
        out, off = b"", 0
        while True:
            chunk = self.fs.read(path, 65536, off, fh)
            if not chunk:
                break
            out, off = out + chunk, off + len(chunk)
        self.fs.release(path, fh)
        return out

    def status(self, path):
        return self.fs.getxattr(path, XATTR_STATUS).decode()

    def ls(self, path="/"):
        return self.fs.readdir(path, 0)[2:]

    def assertErrno(self, code, fn, *args):
        with self.assertRaises(FuseOSError) as cm:
            fn(*args)
        self.assertEqual(cm.exception.errno, code)

    def basic_drive(self):
        d = self.drive
        self.docs = d.r_folder("Docs")
        self.a_id = d.r_create("a.txt", self.docs, b"alpha")
        self.b_id = d.r_create("b.txt", content=b"bravo")
        d.r_create("Notes", mime=DOC_MIME)
        d.r_create("shortcut", mime=SHORTCUT_MIME)
        self.sync()

    # ------------------------------------------------------------------
    def test_whole_drive_is_listed_without_downloading(self):
        self.basic_drive()
        self.assertEqual(self.ls(), ["Docs", "Notes.gdoc", "b.txt"])
        self.assertEqual(self.ls("/Docs"), ["a.txt"])
        st = self.fs.getattr("/Docs/a.txt")
        self.assertEqual(st["st_size"], 5)
        self.assertEqual(st["st_blocks"], 0)  # takes no disk space yet
        self.assertEqual(self.status("/b.txt"), "cloud")
        self.assertEqual(self.status("/Docs"), "cloud")
        self.assertEqual(self.drive.downloads, 0)

    def test_opening_a_file_downloads_it_once(self):
        self.basic_drive()
        self.assertEqual(self.read("/Docs/a.txt"), b"alpha")
        self.assertEqual(self.read("/Docs/a.txt"), b"alpha")
        self.assertEqual(self.drive.downloads, 1)
        self.assertEqual(self.status("/Docs/a.txt"), "local")
        self.assertEqual(self.status("/Docs"), "local")
        self.assertGreater(self.fs.getattr("/Docs/a.txt")["st_blocks"], 0)

    def test_type_sniffing_does_not_download_whole_file(self):
        big = bytes(range(256)) * 1000
        self.drive.r_create("video.bin", content=big)
        self.sync()
        fh = self.fs.open("/video.bin", O_RD)
        self.assertEqual(self.fs.read("/video.bin", 4096, 0, fh), big[:4096])
        self.fs.release("/video.bin", fh)
        self.assertEqual((self.drive.downloads, self.drive.ranges), (0, 1))
        self.assertEqual(self.status("/video.bin"), "cloud")
        self.assertEqual(self.read("/video.bin"), big)  # a real read fetches it
        self.assertEqual(self.drive.downloads, 1)

    def test_thumbnailers_cannot_trigger_downloads(self):
        self.basic_drive()
        self.fs.caller = lambda: "gdk-pixbuf-thum"
        self.assertErrno(errno.EACCES, self.fs.open, "/b.txt", O_RD)
        self.fs.caller = lambda: "tracker-extract"
        self.assertErrno(errno.EACCES, self.fs.open, "/b.txt", O_RD)
        self.assertEqual(self.drive.downloads, 0)
        self.fs.caller = lambda: "eog"
        self.read("/b.txt")
        self.fs.caller = lambda: "gdk-pixbuf-thum"
        self.assertEqual(self.read("/b.txt"), b"bravo")  # already local: fine

    def test_new_files_and_folders_are_uploaded(self):
        self.basic_drive()
        self.fs.mkdir("/Work", 0o755)
        self.write("/Work/plan.txt", b"hello")
        self.assertEqual(self.status("/Work/plan.txt"), "syncing")
        self.assertEqual(self.status("/Work"), "syncing")
        self.sync()
        work = self.drive.child("Work")
        self.assertEqual(self.drive.child("plan.txt", work["id"])["content"], b"hello")
        self.assertEqual(self.status("/Work/plan.txt"), "local")
        self.assertEqual(self.status("/Work"), "local")

    def test_editing_updates_the_same_drive_file(self):
        self.basic_drive()
        fh = self.fs.open("/b.txt", O_RW)
        self.fs.write("/b.txt", b"BRAVO!", 0, fh)
        self.assertEqual(self.fs.getattr("/b.txt")["st_size"], 6)  # live size while open
        self.fs.release("/b.txt", fh)
        self.sync()
        self.assertEqual(self.drive.files[self.b_id]["content"], b"BRAVO!")
        self.assertEqual(self.drive.uploads, 1)

    def test_atomic_save_keeps_drive_file_identity(self):
        self.basic_drive()
        self.write("/.goutputstream-X1Y2", b"saved by gedit")
        self.fs.rename("/.goutputstream-X1Y2", "/b.txt")
        self.sync()
        self.assertEqual(self.drive.files[self.b_id]["content"], b"saved by gedit")
        self.assertEqual([f["name"] for f in self.drive.live() if f["name"].startswith(".")], [])
        self.assertEqual(self.drive.uploads, 1)
        self.assertEqual(self.ls(), ["Docs", "Notes.gdoc", "b.txt"])

    def test_rename_move_and_delete_reach_drive(self):
        self.basic_drive()
        self.fs.rename("/b.txt", "/Docs/c.txt")
        self.fs.rename("/Docs", "/Documents")
        self.sync()
        self.assertEqual(self.drive.files[self.b_id]["name"], "c.txt")
        self.assertEqual(self.drive.files[self.b_id]["parents"], [self.docs])
        self.assertEqual(self.drive.files[self.docs]["name"], "Documents")
        self.fs.unlink("/Documents/a.txt")
        self.fs.unlink("/Documents/c.txt")
        self.fs.rmdir("/Documents")
        self.sync()
        self.assertTrue(self.drive.files[self.docs]["trashed"])
        self.assertEqual(self.drive.uploads, 0)
        self.assertEqual(self.drive.downloads, 0)  # moving/deleting never needs the content

    def test_drive_changes_show_up(self):
        self.basic_drive()
        self.read("/b.txt")
        d = self.drive
        d.r_create("from-phone.jpg", self.docs, b"jpeg")
        d.r_modify(self.b_id, b"bravo v2")
        d.r_rename(self.a_id, "alpha.txt")
        self.sync()
        self.assertEqual(self.ls("/Docs"), ["alpha.txt", "from-phone.jpg"])
        self.assertEqual(self.status("/b.txt"), "cloud")  # stale local copy dropped
        self.assertEqual(self.read("/b.txt"), b"bravo v2")
        d.r_trash(self.docs)
        self.sync()
        self.assertEqual(self.ls(), ["Notes.gdoc", "b.txt"])

    def test_edit_on_both_sides_keeps_both_versions(self):
        self.basic_drive()
        fh = self.fs.open("/b.txt", O_RW)
        self.fs.write("/b.txt", b"laptop edit", 0, fh)
        self.fs.release("/b.txt", fh)
        self.drive.r_modify(self.b_id, b"phone edit")
        self.sync()
        self.assertEqual(self.read("/b.txt"), b"phone edit")
        copy = [n for n in self.ls() if n.startswith("b (conflict")]
        self.assertEqual(len(copy), 1)
        self.assertEqual(self.read("/" + copy[0]), b"laptop edit")
        self.assertEqual(self.drive.child(copy[0])["content"], b"laptop edit")
        self.assertEqual(len(self.engine.conflicts), 1)

    def test_drive_delete_keeps_unsynced_local_edit(self):
        self.basic_drive()
        fh = self.fs.open("/b.txt", O_RW | os.O_TRUNC)
        self.fs.write("/b.txt", b"mine", 0, fh)
        self.fs.release("/b.txt", fh)
        self.drive.r_trash(self.b_id)
        self.sync()
        self.assertEqual(self.read("/b.txt"), b"mine")
        self.assertEqual(self.drive.child("b.txt")["content"], b"mine")

    def test_available_offline_and_free_up_space(self):
        self.basic_drive()
        self.drive.r_create("big.pdf", self.docs, b"%PDF")
        self.sync()
        self.fs.setxattr("/Docs", XATTR_ACTION, b"pin", 0)
        self.sync()
        self.assertEqual(self.drive.downloads, 2)
        self.assertEqual(self.status("/Docs/big.pdf"), "pinned")
        self.assertEqual(self.status("/Docs"), "pinned")
        self.assertEqual(self.status("/b.txt"), "cloud")
        self.fs.setxattr("/Docs", XATTR_ACTION, b"free", 0)
        self.assertEqual(self.status("/Docs/big.pdf"), "cloud")
        self.assertEqual(list((self.tmp / "cache").iterdir()), [])

    def test_keep_everything_offline_is_mirror_mode(self):
        self.basic_drive()
        self.engine.set_keep_all_offline(True)
        self.sync()
        self.assertEqual(self.drive.downloads, 2)  # Google Docs links need no download
        self.assertEqual(self.status("/Docs/a.txt"), "pinned")

    def test_cache_limit_evicts_oldest_opened_files(self):
        self.config.update(cache_limit_gb=1000 / 1024**3)
        self.drive.r_create("one.bin", content=b"1" * 800)
        self.drive.r_create("two.bin", content=b"2" * 800)
        self.sync()
        self.read("/one.bin")
        self.read("/two.bin")
        self.sync()
        self.assertEqual(self.status("/one.bin"), "cloud")
        self.assertEqual(self.status("/two.bin"), "local")

    def test_mass_delete_needs_confirmation_and_can_be_undone(self):
        self.config.update(mass_delete_threshold=3)
        for i in range(5):
            self.drive.r_create(f"f{i}.txt", content=b"x")
        self.sync()
        for name in self.ls():
            self.fs.unlink("/" + name)
        self.sync()
        self.assertEqual(len(self.drive.live()), 5)
        self.assertEqual(self.engine.pending_mass_delete["count"], 5)
        self.engine.restore_deleted()
        self.assertEqual(len(self.ls()), 5)
        for name in self.ls():
            self.fs.unlink("/" + name)
        self.engine.confirm_mass_delete()
        self.sync()
        self.assertEqual(self.drive.live(), [])

    def test_unsafe_and_duplicate_names_are_made_safe(self):
        d = self.drive
        d.r_create("dup.txt", content=b"1")
        d.r_create("dup.txt", content=b"2")
        d.r_create("..", content=b"dots")
        d.r_create("../../escape.txt", content=b"evil")
        self.sync()
        self.assertEqual(sorted(self.ls()), [".._.._escape.txt", "_..", "dup (1).txt", "dup.txt"])
        self.sync()
        self.assertEqual((self.drive.uploads, len(self.drive.live())), (0, 4))

    def test_temporary_files_stay_local(self):
        self.basic_drive()
        self.write("/notes.txt.swp", b"vim")
        self.write("/.~lock.report.odt#", b"lo")
        self.sync()
        self.assertEqual(self.drive.uploads, 0)
        self.assertEqual(self.status("/notes.txt.swp"), "local")
        self.assertEqual(self.status("/"), "cloud")  # temp files don't keep folders "syncing"

    def test_google_docs_are_read_only_links(self):
        self.basic_drive()
        link = json.loads(self.read("/Notes.gdoc"))
        self.assertTrue(link["url"].startswith("https://docs.google.com/"))
        self.assertErrno(errno.EACCES, self.fs.open, "/Notes.gdoc", O_RW)

    def test_folder_is_read_only_until_drive_is_connected(self):
        self.assertErrno(errno.EROFS, self.fs.create, "/early.txt", 0o644)
        self.assertErrno(errno.EROFS, self.fs.mkdir, "/early", 0o755)

    def test_offline_edits_are_uploaded_later(self):
        self.basic_drive()
        self.read("/b.txt")
        real = self.drive.upload_update

        def offline(*a, **k):
            import requests
            raise requests.ConnectionError("no network")

        self.drive.upload_update = offline
        fh = self.fs.open("/b.txt", O_RW)
        self.fs.write("/b.txt", b"written offline", 0, fh)
        self.fs.release("/b.txt", fh)
        with self.assertRaises(Exception):
            self.sync()
        self.assertEqual(self.read("/b.txt"), b"written offline")
        self.drive.upload_update = real
        self.sync()
        self.assertEqual(self.drive.files[self.b_id]["content"], b"written offline")


if __name__ == "__main__":
    unittest.main()
