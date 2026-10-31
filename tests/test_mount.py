"""End-to-end test through a real FUSE mount (skipped if FUSE is unavailable)."""

import errno
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

_tmp_home = tempfile.mkdtemp()
os.environ.setdefault("XDG_CONFIG_HOME", os.path.join(_tmp_home, "config"))
os.environ.setdefault("XDG_DATA_HOME", os.path.join(_tmp_home, "data"))

from fakes import MANAGER, VIEWER, FakeAuth, FakeConfig, FakeDrive  # noqa: E402
from gdrive_sync.engine import Engine  # noqa: E402
from gdrive_sync.store import Store  # noqa: E402

HAVE_FUSE = os.path.exists("/dev/fuse") and shutil.which("fusermount3")


@unittest.skipUnless(HAVE_FUSE, "FUSE is not available")
class MountTestCase(unittest.TestCase):
    def setUp(self):
        from mfusepy import FUSE

        from gdrive_sync.vfs import DriveFS

        self.tmp = Path(tempfile.mkdtemp())
        self.mnt = self.tmp / "GoogleDrive"
        self.mnt.mkdir()
        self.drive = FakeDrive()
        self.docs = self.drive.r_folder("Docs")
        self.hello = self.drive.r_create("hello.txt", content=b"hi there")
        self.drive.r_create("report.pdf", self.docs, b"%PDF-1.7 ...")
        self.engine = Engine(FakeConfig(self.mnt), FakeAuth(), self.drive, Store(":memory:"), self.tmp / "cache")
        self.engine.settle_seconds = 0
        self.engine.sync_once()
        fs = DriveFS(self.engine)
        self.thread = threading.Thread(
            target=lambda: FUSE(fs, str(self.mnt), foreground=True, nothreads=False, fsname="gdrive-test",
                                default_permissions=True),
            daemon=True,
        )
        self.thread.start()
        for _ in range(100):
            if os.path.ismount(self.mnt):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.ismount(self.mnt), "mount did not appear")

    def tearDown(self):
        subprocess.run(["fusermount3", "-u", "-z", str(self.mnt)], capture_output=True)
        self.thread.join(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self, rel):
        return os.getxattr(self.mnt / "My Drive" / rel, "user.gdrive.status").decode()

    def test_files_behave_like_a_normal_folder(self):
        m = self.mnt / "My Drive"
        self.assertEqual(sorted(os.listdir(self.mnt)), [f".Trash-{os.getuid()}", "My Drive", "Shared drives"])
        self.assertEqual(sorted(os.listdir(m)), ["Docs", "hello.txt"])
        self.assertEqual(os.stat(m / "Docs" / "report.pdf").st_size, 12)
        self.assertEqual(oct(os.stat(m).st_mode & 0o777), "0o700")
        self.assertEqual(self.status("hello.txt"), "cloud")
        self.assertEqual(self.drive.downloads, 0)

        # opening downloads on demand
        self.assertEqual((m / "hello.txt").read_bytes(), b"hi there")
        self.assertEqual(self.status("hello.txt"), "local")
        self.assertEqual(self.drive.downloads, 1)

        # new folder + file, then an editor-style atomic save over an existing file
        (m / "Notes").mkdir()
        (m / "Notes" / "todo.md").write_text("- buy milk\n")
        tmp = m / ".goutputstream-ABC123"
        tmp.write_bytes(b"hi there, edited")
        os.replace(tmp, m / "hello.txt")
        self.assertEqual(self.status("Notes/todo.md"), "syncing")
        self.engine.sync_once()
        notes = self.drive.child("Notes")
        self.assertEqual(self.drive.child("todo.md", notes["id"])["content"], b"- buy milk\n")
        self.assertEqual(self.drive.files[self.hello]["content"], b"hi there, edited")
        self.assertEqual(self.status("Notes/todo.md"), "local")

        # copy out, rename, append, delete
        shutil.copy(m / "Docs" / "report.pdf", self.tmp / "copy.pdf")
        self.assertEqual((self.tmp / "copy.pdf").read_bytes(), b"%PDF-1.7 ...")
        os.rename(m / "Notes" / "todo.md", m / "Docs" / "todo.md")
        with open(m / "Docs" / "todo.md", "a") as f:
            f.write("- call mum\n")
        (m / "hello.txt").unlink()
        self.engine.sync_once()
        todo = self.drive.child("todo.md", self.docs)
        self.assertEqual(todo["content"], b"- buy milk\n- call mum\n")
        self.assertTrue(self.drive.files[self.hello]["trashed"])

        # file manager actions via extended attributes
        os.setxattr(m / "Docs", "user.gdrive.action", b"free")
        self.assertEqual(self.status("Docs/report.pdf"), "cloud")
        os.setxattr(m / "Docs", "user.gdrive.action", b"pin")
        self.engine.sync_once()
        self.assertEqual(self.status("Docs/report.pdf"), "pinned")
        self.assertTrue(os.getxattr(m / "Docs", "user.gdrive.link").startswith(b"https://drive.google.com/"))
        gen = int(os.getxattr(self.mnt, "user.gdrive.generation"))
        self.drive.r_create("new-from-phone.jpg", content=b"jpg")
        self.engine.sync_once()
        self.assertGreater(int(os.getxattr(self.mnt, "user.gdrive.generation")), gen)
        self.assertIn("new-from-phone.jpg", os.listdir(m))

    def test_shared_drives_follow_your_role(self):
        d, shared = self.drive, self.mnt / "Shared drives"
        team = d.r_drive("Team")
        d.r_create("plan.txt", team, b"the plan", caps=MANAGER)
        locked = d.r_drive("Locked", can_add=False)
        d.r_create("policy.txt", locked, b"read me", caps=VIEWER)
        self.engine.sync_once()
        self.assertEqual(sorted(os.listdir(shared)), ["Locked", "Team"])

        # read-write drive: reading downloads, writing uploads into the shared drive
        self.assertEqual((shared / "Team" / "plan.txt").read_bytes(), b"the plan")
        (shared / "Team" / "new.txt").write_text("added here")
        self.engine.sync_once()
        self.assertEqual(d.child("new.txt", team)["driveId"], team)

        # view-only drive: readable, but the kernel refuses every change
        self.assertEqual((shared / "Locked" / "policy.txt").read_bytes(), b"read me")
        with self.assertRaises(PermissionError):
            (shared / "Locked" / "policy.txt").write_text("nope")
        with self.assertRaises(PermissionError):
            (shared / "Locked" / "new.txt").write_text("nope")
        with self.assertRaises(PermissionError):
            (shared / "Locked" / "policy.txt").unlink()
        with self.assertRaises(PermissionError):
            (self.mnt / "stray.txt").write_text("nope")

        # moving between drives is a copy followed by a delete, which `mv` and Files do by themselves
        with self.assertRaises(OSError) as cm:
            os.rename(shared / "Team" / "new.txt", self.mnt / "My Drive" / "new.txt")
        self.assertEqual(cm.exception.errno, errno.EXDEV)
        shutil.move(shared / "Team" / "new.txt", self.mnt / "My Drive" / "new.txt")
        self.engine.sync_once()
        self.assertEqual(d.child("new.txt")["content"], b"added here")
        self.assertIsNone(d.child("new.txt", team))

    def gio_trash(self, path):
        return subprocess.run(["gio", "trash", str(path)], capture_output=True, text=True)

    @unittest.skipUnless(shutil.which("gio"), "gio is not available")
    def test_delete_key_in_files_moves_to_the_drive_trash(self):
        d, m = self.drive, self.mnt / "My Drive"
        team = d.r_drive("Team")
        shared_file = d.r_create("team.txt", team, b"t", caps=MANAGER)
        folder = d.r_folder("Project", team, caps=MANAGER)
        inner = d.r_create("inner.txt", folder, b"i", caps=MANAGER)
        locked = d.r_drive("Locked", can_add=False)
        policy = d.r_create("policy.txt", locked, b"p", caps=VIEWER)
        self.engine.sync_once()
        shared = self.mnt / "Shared drives"

        self.assertEqual(self.gio_trash(m / "hello.txt").returncode, 0)
        self.assertEqual(self.gio_trash(shared / "Team" / "team.txt").returncode, 0)
        self.assertEqual(self.gio_trash(shared / "Team" / "Project").returncode, 0)
        self.assertEqual(sorted(os.listdir(m)), ["Docs"])
        self.assertEqual(os.listdir(shared / "Team"), [])
        self.assertEqual(self.gio_trash(shared / "Locked" / "policy.txt").returncode, 1)
        self.assertEqual(os.listdir(shared / "Locked"), ["policy.txt"])

        self.engine.sync_once()
        for fid in (self.hello, shared_file, folder):
            self.assertTrue(d.files[fid]["trashed"])
        self.assertFalse(d.files[policy]["trashed"])
        self.assertEqual(self.engine._pending(), [])
        self.assertEqual(os.listdir(self.mnt / f".Trash-{os.getuid()}" / "info"), [])
        self.assertEqual(os.listdir(self.mnt / f".Trash-{os.getuid()}" / "files"), [])
        # trashing the folder takes its contents along in Drive: no separate call for each file
        self.assertFalse(d.files[inner]["trashed"])


if __name__ == "__main__":
    unittest.main()
