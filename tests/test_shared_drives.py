"""Shared drives: layout, change feeds, roles, and upgrading from the My Drive-only version."""

import errno
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_tmp_home = tempfile.mkdtemp()
os.environ.setdefault("XDG_CONFIG_HOME", os.path.join(_tmp_home, "config"))
os.environ.setdefault("XDG_DATA_HOME", os.path.join(_tmp_home, "data"))

from mfusepy import FuseOSError  # noqa: E402

from fakes import CONTRIBUTOR, MANAGER, NO_DOWNLOAD, VIEWER, FakeAuth, FakeConfig, FakeDrive  # noqa: E402
from gdrive_sync.engine import Engine  # noqa: E402
from gdrive_sync.store import ROOT, Store  # noqa: E402
from gdrive_sync.vfs import XATTR_ACTION, XATTR_LINK, XATTR_STATUS, DriveFS  # noqa: E402

TEAM = "/Shared drives/Team"


class SharedDriveTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, HOME=str(self.tmp))  # unsynced files are rescued to $HOME
        patcher.start()
        self.addCleanup(patcher.stop)
        self.drive = FakeDrive()
        self.config = FakeConfig(self.tmp / "GoogleDrive")
        self.start_engine(Store(":memory:"))

    def start_engine(self, store):
        self.store = store
        self.engine = Engine(self.config, FakeAuth(), self.drive, store, self.tmp / "cache")
        self.engine.settle_seconds = 0
        self.fs = DriveFS(self.engine)
        self.fs.caller = lambda: "python3"

    def sync(self):
        self.engine.sync_once()

    def ls(self, path="/"):
        return [n for n in self.fs.readdir(path, 0)[2:] if not n.startswith(".Trash-")]

    def read(self, path):
        fh = self.fs.open(path, os.O_RDONLY)
        data = self.fs.read(path, 1 << 20, 0, fh)
        self.fs.release(path, fh)
        return data

    def write(self, path, data):
        try:
            self.fs.getattr(path)
            fh = self.fs.open(path, os.O_WRONLY | os.O_TRUNC)
        except FuseOSError as e:
            if e.errno != errno.ENOENT:
                raise
            fh = self.fs.create(path, 0o644)
        self.fs.write(path, data, 0, fh)
        self.fs.release(path, fh)

    def status(self, path):
        return self.fs.getxattr(path, XATTR_STATUS).decode()

    def mode(self, path):
        return self.fs.getattr(path)["st_mode"] & 0o777

    def assertErrno(self, code, fn, *args):
        with self.assertRaises(FuseOSError) as cm:
            fn(*args)
        self.assertEqual(cm.exception.errno, code)

    def team_drive(self, file_caps=MANAGER, can_add=True):
        d = self.drive
        self.team = d.r_drive("Team", can_add=can_add)
        self.specs = d.r_folder("Specs", self.team, caps=dict(file_caps, canAddChildren=can_add))
        self.doc = d.r_create("spec.txt", self.specs, b"version 1", caps=file_caps)
        self.top = d.r_create("readme.txt", self.team, b"hello team", caps=file_caps)

    # -------------------------------------------------------- Delete in Files
    def trash_path(self, name):
        return f"/.Trash-{os.getuid()}/{name}"

    def press_delete(self, path, name):
        """What GNOME does: write a note, then move the item into the trash folder."""
        note = self.trash_path(f"info/{name}.trashinfo")
        self.fs.release(note, self.fs.create(note, 0o600))
        fh = self.fs.create(f"{note}.TMP123", 0o600)
        self.fs.write("", b"[Trash Info]", 0, fh)
        self.fs.rename(f"{note}.TMP123", note)
        self.fs.release("", fh)
        self.fs.rename(path, self.trash_path(f"files/{name}"))

    def test_deleting_a_folder_in_files_trashes_it_in_drive(self):
        self.team_drive()
        self.sync()
        self.read(f"{TEAM}/Specs/spec.txt")
        self.press_delete(f"{TEAM}/Specs", "Specs")
        self.assertEqual(self.ls(TEAM), ["readme.txt"])
        self.assertEqual(self.ls(self.trash_path("files")), [])
        self.assertEqual(self.ls(self.trash_path("info")), [])
        self.sync()
        self.assertTrue(self.drive.files[self.specs]["trashed"])
        self.assertEqual(self.engine._pending(), [])
        self.assertEqual(self.store.count("WHERE name='spec.txt'"), 0)

    def test_mass_delete_guard_covers_folders_deleted_in_files(self):
        self.config.update(mass_delete_threshold=1)
        self.team_drive()
        self.drive.r_create("more.txt", self.specs, b"x", caps=MANAGER)
        self.sync()
        self.press_delete(f"{TEAM}/Specs", "Specs")
        self.sync()
        self.assertFalse(self.drive.files[self.specs]["trashed"])
        self.engine.restore_deleted()
        self.assertEqual(self.ls(f"{TEAM}/Specs"), ["more.txt", "spec.txt"])

    def test_deleting_unsynced_work_keeps_a_copy(self):
        self.team_drive()
        self.sync()
        self.write(f"{TEAM}/draft.txt", b"not uploaded")
        self.press_delete(f"{TEAM}/draft.txt", "draft.txt")
        self.assertEqual(self.engine._pending(), [])
        rescued = list(self.tmp.glob("GDrive unsynced files */Shared drives/Team/draft.txt"))
        self.assertEqual(rescued[0].read_bytes(), b"not uploaded")

    def test_trash_folders_accept_nothing_else(self):
        self.sync()
        self.assertErrno(errno.EACCES, self.fs.create, self.trash_path("files/x.txt"), 0o600)
        self.assertErrno(errno.EACCES, self.fs.create, self.trash_path("info/x.txt"), 0o600)
        self.assertErrno(errno.EACCES, self.fs.create, self.trash_path("x.txt"), 0o600)
        self.assertErrno(errno.EACCES, self.fs.mkdir, self.trash_path("info/x"), 0o700)
        self.assertErrno(errno.EACCES, self.fs.rmdir, self.trash_path("files"))
        self.assertEqual(self.mode(self.trash_path("info")), 0o700)

    # ------------------------------------------------------------- layout
    def test_my_drive_and_shared_drives_side_by_side_without_downloading(self):
        self.drive.r_create("mine.txt", content=b"mine")
        self.team_drive()
        self.drive.r_drive("Empty")
        self.sync()
        self.assertEqual(self.ls(), ["My Drive", "Shared drives"])
        self.assertEqual(self.ls("/My Drive"), ["mine.txt"])
        self.assertEqual(self.ls("/Shared drives"), ["Empty", "Team"])
        self.assertEqual(self.ls(TEAM), ["Specs", "readme.txt"])
        self.assertEqual(self.fs.getattr(f"{TEAM}/Specs/spec.txt")["st_size"], 9)
        self.assertEqual(self.status(f"{TEAM}/Specs/spec.txt"), "cloud")
        self.assertEqual(self.drive.downloads, 0)
        self.assertEqual(self.engine.status()["shared_drives"], 2)

    def test_drives_with_the_same_name_stay_separate(self):
        a, b = self.drive.r_drive("Projects"), self.drive.r_drive("Projects")
        self.drive.r_create("a.txt", a, b"a")
        self.drive.r_create("b.txt", b, b"b")
        self.sync()
        self.assertEqual(self.ls("/Shared drives"), ["Projects", "Projects (1)"])
        self.assertEqual(self.ls("/Shared drives/Projects (1)"), ["b.txt"])

    def test_folders_inside_a_drive_named_like_the_top_folders_are_fine(self):
        self.drive.r_folder("My Drive")
        self.drive.r_folder("Shared drives")
        self.sync()
        self.assertEqual(self.ls("/My Drive"), ["My Drive", "Shared drives"])

    def test_top_level_folders_are_protected(self):
        self.team_drive()
        self.sync()
        self.assertEqual(self.mode("/"), 0o500)
        self.assertEqual(self.mode("/Shared drives"), 0o500)
        self.assertEqual(self.mode("/My Drive"), 0o700)
        self.assertEqual(self.mode(TEAM), 0o700)
        self.assertErrno(errno.EACCES, self.fs.create, "/stray.txt", 0o644)
        self.assertErrno(errno.EACCES, self.fs.mkdir, "/Shared drives/New", 0o755)
        self.assertErrno(errno.EACCES, self.fs.rename, "/My Drive", "/Mine")
        self.assertErrno(errno.EACCES, self.fs.rename, TEAM, "/Shared drives/Renamed")
        self.fs.unlink(f"{TEAM}/readme.txt")
        self.fs.unlink(f"{TEAM}/Specs/spec.txt")
        self.fs.rmdir(f"{TEAM}/Specs")
        self.assertErrno(errno.EACCES, self.fs.rmdir, TEAM)
        self.assertErrno(errno.EACCES, self.fs.rmdir, "/My Drive")

    def test_open_in_browser_links(self):
        self.team_drive()
        self.sync()
        self.assertEqual(self.fs.getxattr("/My Drive", XATTR_LINK), b"https://drive.google.com/drive/my-drive")
        self.assertIn(self.team.encode(), self.fs.getxattr(TEAM, XATTR_LINK))
        self.assertErrno(errno.ENODATA, self.fs.getxattr, "/Shared drives", XATTR_LINK)

    # ---------------------------------------------------------- two-way sync
    def test_reading_and_writing_inside_a_shared_drive(self):
        self.team_drive()
        self.sync()
        self.assertEqual(self.read(f"{TEAM}/Specs/spec.txt"), b"version 1")
        self.assertEqual(self.status(f"{TEAM}/Specs/spec.txt"), "local")

        self.write(f"{TEAM}/Specs/spec.txt", b"version 2")
        self.fs.mkdir(f"{TEAM}/Minutes", 0o700)
        self.write(f"{TEAM}/Minutes/monday.txt", b"notes")
        self.fs.rename(f"{TEAM}/readme.txt", f"{TEAM}/Specs/README.txt")
        self.sync()
        d = self.drive
        self.assertEqual(d.files[self.doc]["content"], b"version 2")
        minutes = d.child("Minutes", self.team)
        self.assertEqual(minutes["driveId"], self.team)
        self.assertEqual(d.child("monday.txt", minutes["id"])["content"], b"notes")
        self.assertEqual((d.files[self.top]["name"], d.files[self.top]["parents"]), ("README.txt", [self.specs]))
        self.assertEqual(self.engine._pending(), [])

        self.fs.unlink(f"{TEAM}/Specs/spec.txt")
        self.sync()
        self.assertTrue(d.files[self.doc]["trashed"])

    def test_drive_changes_arrive_from_each_feed(self):
        self.drive.r_create("mine.txt", content=b"mine")
        self.team_drive()
        other = self.drive.r_drive("Other")
        self.sync()
        d = self.drive
        d.r_modify(self.doc, b"edited by a colleague")
        d.r_create("new.pdf", self.team, b"%PDF")
        d.r_create("other.txt", other, b"x")
        d.r_create("mine2.txt", content=b"y")
        d.r_trash(self.top)
        self.sync()
        self.assertEqual(self.read(f"{TEAM}/Specs/spec.txt"), b"edited by a colleague")
        self.assertEqual(self.ls(TEAM), ["Specs", "new.pdf"])
        self.assertEqual(self.ls("/Shared drives/Other"), ["other.txt"])
        self.assertEqual(self.ls("/My Drive"), ["mine.txt", "mine2.txt"])

    def test_both_sides_edited_in_a_shared_drive_keeps_both(self):
        self.team_drive()
        self.sync()
        self.write(f"{TEAM}/readme.txt", b"mine")
        self.drive.r_modify(self.top, b"theirs")
        self.sync()
        self.assertEqual(self.read(f"{TEAM}/readme.txt"), b"theirs")
        copy = [n for n in self.ls(TEAM) if "conflict" in n]
        self.assertEqual(self.drive.child(copy[0], self.team)["content"], b"mine")

    def test_file_moved_between_drives_in_drive_is_not_lost(self):
        self.team_drive()
        other = self.drive.r_drive("Alpha")  # sorts first, so its feed is read before Team's
        late = self.drive.r_drive("Zulu")    # sorts last, so its feed is read after Team's
        self.sync()
        self.read(f"{TEAM}/readme.txt")
        self.drive.r_move_to_drive(self.top, other)
        self.sync()
        self.assertEqual(self.ls("/Shared drives/Alpha"), ["readme.txt"])
        self.assertEqual(self.read("/Shared drives/Alpha/readme.txt"), b"hello team")
        self.drive.r_move_to_drive(self.doc, late)
        self.sync()
        self.assertEqual(self.ls("/Shared drives/Zulu"), ["spec.txt"])
        self.assertEqual(self.ls(f"{TEAM}/Specs"), [])

    def test_moving_between_drives_is_a_copy_for_programs(self):
        self.team_drive()
        self.drive.r_drive("Other")
        self.sync()
        self.assertErrno(errno.EXDEV, self.fs.rename, f"{TEAM}/readme.txt", "/My Drive/readme.txt")
        self.assertErrno(errno.EXDEV, self.fs.rename, f"{TEAM}/Specs", "/Shared drives/Other/Specs")
        self.assertEqual(self.drive.files[self.top]["parents"], [self.team])

    # ----------------------------------------------------------------- roles
    def test_viewer_can_read_but_not_change(self):
        self.team_drive(file_caps=VIEWER, can_add=False)
        self.sync()
        spec = f"{TEAM}/Specs/spec.txt"
        self.assertEqual(self.mode(spec), 0o400)
        self.assertEqual(self.mode(f"{TEAM}/Specs"), 0o500)
        self.assertEqual(self.mode(TEAM), 0o500)
        self.assertEqual(self.read(spec), b"version 1")
        self.assertErrno(errno.EACCES, self.fs.open, spec, os.O_RDWR)
        self.assertErrno(errno.EACCES, self.fs.truncate, spec, 0)
        self.assertErrno(errno.EACCES, self.fs.create, f"{TEAM}/new.txt", 0o644)
        self.assertErrno(errno.EACCES, self.fs.unlink, spec)
        self.assertErrno(errno.EACCES, self.fs.rename, spec, f"{TEAM}/Specs/x.txt")
        self.assertEqual(self.engine._pending(), [])

    def test_contributor_can_add_and_edit_but_not_delete_or_move(self):
        self.team_drive(file_caps=CONTRIBUTOR)
        self.sync()
        self.write(f"{TEAM}/readme.txt", b"edited")
        self.write(f"{TEAM}/added.txt", b"new")
        self.assertErrno(errno.EACCES, self.fs.unlink, f"{TEAM}/Specs/spec.txt")
        self.assertErrno(errno.EACCES, self.fs.rename, f"{TEAM}/Specs/spec.txt", f"{TEAM}/spec.txt")
        self.fs.unlink(f"{TEAM}/added.txt")  # not in Drive yet, so it's still yours to delete
        self.sync()
        self.assertEqual(self.drive.files[self.top]["content"], b"edited")

    def test_saving_over_a_read_only_file_is_refused(self):
        self.team_drive(file_caps=VIEWER)
        self.sync()
        self.write(f"{TEAM}/.goutputstream-1", b"attempted save")
        self.assertErrno(errno.EACCES, self.fs.rename, f"{TEAM}/.goutputstream-1", f"{TEAM}/readme.txt")
        self.assertEqual(self.drive.files[self.top]["content"], b"hello team")

    def test_files_that_viewers_may_not_download(self):
        self.team_drive(file_caps=NO_DOWNLOAD)
        self.sync()
        self.assertErrno(errno.EACCES, self.fs.open, f"{TEAM}/readme.txt", os.O_RDONLY)
        self.fs.setxattr(TEAM, XATTR_ACTION, b"pin", 0)
        self.sync()
        self.assertEqual(self.drive.downloads, 0)
        self.assertEqual(self.engine.activity[0]["kind"], "info")  # no repeating errors

    def test_role_changes_are_picked_up(self):
        self.team_drive(file_caps=MANAGER)
        self.sync()
        self.drive.files[self.top]["capabilities"] = VIEWER
        self.drive._touch(self.top)
        self.sync()
        self.assertEqual(self.mode(f"{TEAM}/readme.txt"), 0o400)

    # ----- Google refuses something we thought was allowed (a role changed)
    def test_refused_delete_puts_the_file_back(self):
        self.team_drive()
        self.sync()
        self.fs.unlink(f"{TEAM}/readme.txt")
        self.drive.denied = {"trash"}
        self.sync()
        self.assertIn("readme.txt", self.ls(TEAM))
        self.assertEqual(self.engine._pending(), [])
        self.assertFalse(self.drive.files[self.top]["trashed"])
        self.assertEqual(self.read(f"{TEAM}/readme.txt"), b"hello team")

    def test_refused_rename_is_undone(self):
        self.team_drive()
        self.sync()
        self.fs.rename(f"{TEAM}/readme.txt", f"{TEAM}/Specs/moved.txt")
        self.drive.denied = {"move"}
        self.sync()
        self.assertIn("readme.txt", self.ls(TEAM))
        self.assertEqual(self.ls(f"{TEAM}/Specs"), ["spec.txt"])
        self.assertEqual(self.engine._pending(), [])

    def test_refused_edit_keeps_your_version_as_a_copy(self):
        self.team_drive()
        self.sync()
        self.write(f"{TEAM}/readme.txt", b"my edit")
        self.drive.denied = {"edit"}
        self.sync()
        self.sync()  # the copy is a new file: it goes up on the next pass
        self.assertEqual(self.drive.files[self.top]["content"], b"hello team")
        self.assertEqual(self.read(f"{TEAM}/readme.txt"), b"hello team")
        copy = [n for n in self.ls(TEAM) if "conflict" in n]
        self.assertEqual(self.drive.child(copy[0], self.team)["content"], b"my edit")

    def test_refused_new_file_is_rescued(self):
        self.team_drive()
        self.sync()
        self.write(f"{TEAM}/new.txt", b"precious")
        self.drive.denied = {"create"}
        self.sync()
        self.assertNotIn("new.txt", self.ls(TEAM))
        rescued = list(self.tmp.glob("GDrive unsynced files */Shared drives/Team/new.txt"))
        self.assertEqual(rescued[0].read_bytes(), b"precious")
        self.assertEqual(self.engine._pending(), [])

    # ---------------------------------------------------- membership changes
    def test_new_renamed_and_removed_drives(self):
        self.team_drive()
        self.sync()
        late = self.drive.r_drive("Joined later")
        self.drive.r_create("hi.txt", late, b"hi")
        self.sync()
        self.assertEqual(self.ls("/Shared drives"), ["Joined later", "Team"])
        self.assertEqual(self.ls("/Shared drives/Joined later"), ["hi.txt"])

        self.drive.drives[late]["name"] = "Renamed"
        self.sync()
        self.assertEqual(self.ls("/Shared drives"), ["Renamed", "Team"])
        self.assertEqual(self.ls("/Shared drives/Renamed"), ["hi.txt"])

        self.read(f"{TEAM}/readme.txt")
        self.write(f"{TEAM}/Specs/spec.txt", b"not uploaded yet")
        del self.drive.drives[self.team]
        self.sync()
        self.assertEqual(self.ls("/Shared drives"), ["Renamed"])
        rescued = list(self.tmp.glob("GDrive unsynced files */Shared drives/Team/Specs/spec.txt"))
        self.assertEqual(rescued[0].read_bytes(), b"not uploaded yet")
        self.assertEqual(self.store.count("WHERE name='spec.txt'"), 0)
        self.assertEqual(list((self.tmp / "cache").iterdir()), [])

    def test_a_drive_that_fails_to_list_does_not_stop_the_others(self):
        self.drive.r_create("mine.txt", content=b"mine")
        bad = self.drive.r_drive("Broken")
        self.team_drive()
        real = self.drive.list_all

        def flaky(drive_id=None):
            from gdrive_sync.drive import DriveError
            if drive_id == bad:
                raise DriveError("backend error", 500)
            return real(drive_id)

        self.drive.list_all = flaky
        self.sync()
        self.assertEqual(self.ls("/Shared drives"), ["Team"])
        self.assertEqual(self.ls("/My Drive"), ["mine.txt"])

    def test_interrupted_drive_listing_is_redone(self):
        self.team_drive()
        self.sync()
        self.store.del_meta(f"drive_token:{self.team}")  # as if we crashed before the listing finished
        self.sync()
        self.assertEqual(self.ls(TEAM), ["Specs", "readme.txt"])
        self.assertEqual(self.store.count("WHERE name='readme.txt'"), 1)

    def test_offline_and_mirror_cover_shared_drives(self):
        self.drive.r_create("mine.txt", content=b"mine")
        self.team_drive()
        self.sync()
        self.fs.setxattr(f"{TEAM}/Specs", XATTR_ACTION, b"pin", 0)
        self.sync()
        self.assertEqual(self.status(f"{TEAM}/Specs/spec.txt"), "pinned")
        self.assertEqual(self.status(f"{TEAM}/readme.txt"), "cloud")
        self.engine.set_keep_all_offline(True)
        self.sync()
        self.assertEqual(self.status(f"{TEAM}/readme.txt"), "pinned")
        self.assertEqual(self.status("/My Drive/mine.txt"), "pinned")

    # --------------------------------------------- upgrading a 2.0 database
    def old_database(self):
        """A database exactly as version 2.0 left it, with an open pending change."""
        d = self.drive
        self.old_dir = d.r_folder("Docs")
        self.old_a = d.r_create("a.txt", self.old_dir, b"alpha")
        self.old_b = d.r_create("b.txt", content=b"bravo")
        self.old_gone = d.r_create("gone.txt", content=b"bye")
        self.drive_token = d.start_page_token()
        path = self.tmp / "vfs.db"
        Store(path).conn.close()  # creates the schema
        c = sqlite3.connect(path)
        c.execute("ALTER TABLE nodes DROP COLUMN caps")
        c.execute("UPDATE nodes SET remote_id=? WHERE ino=1", (d.root,))
        rows = [
            (2, self.old_dir, 1, "Docs", 1, None, 0, 0),
            (3, self.old_a, 2, "a.txt", 0, "text/plain", 1, 0),   # opened, so in the cache
            (4, self.old_b, 1, "b.txt", 0, "text/plain", 0, 0),
            (5, self.old_gone, 1, "gone.txt", 0, "text/plain", 0, 1),  # deleted, not yet applied
        ]
        for ino, rid, parent, name, is_dir, mime, cached, deleted in rows:
            md5 = d.files[rid].get("md5Checksum")
            c.execute(
                "INSERT INTO nodes (ino, remote_id, parent, name, is_dir, mime, size, r_parent, r_name, r_md5, cached, deleted)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ino, rid, parent, name, is_dir, mime, int(d.files[rid].get("size", 0)),
                 d.files[rid]["parents"][0], name, md5, cached, deleted),
            )
        c.execute("INSERT INTO meta VALUES ('initialized', '1')")
        c.execute("INSERT INTO meta VALUES ('page_token', ?)", (self.drive_token,))
        c.commit()
        c.close()
        (self.tmp / "cache").mkdir(exist_ok=True)
        (self.tmp / "cache" / "3").write_bytes(b"alpha")
        return path

    def test_upgrade_keeps_files_cache_and_pending_work(self):
        path = self.old_database()
        self.drive.r_drive("Team")
        self.start_engine(Store(path))
        self.assertEqual(self.ls(), ["My Drive", "Shared drives"])
        self.assertEqual(self.ls("/My Drive"), ["Docs", "b.txt"])
        self.assertEqual(self.store.get(ROOT).caps, 0)
        self.assertEqual(self.read("/My Drive/Docs/a.txt"), b"alpha")  # served from the old cache

        self.sync()
        self.assertEqual(self.drive.downloads, 0)
        self.assertEqual(self.ls("/Shared drives"), ["Team"])
        self.assertTrue(self.drive.files[self.old_gone]["trashed"])  # pending delete still applied
        self.write("/My Drive/b.txt", b"bravo 2")
        self.sync()
        self.assertEqual(self.drive.files[self.old_b]["content"], b"bravo 2")
        self.drive.r_create("after.txt", self.old_dir, b"new")
        self.sync()
        self.assertEqual(self.ls("/My Drive/Docs"), ["a.txt", "after.txt"])

    def test_upgrade_learns_permissions_of_existing_files(self):
        path = self.old_database()
        self.drive.files[self.old_b]["capabilities"] = VIEWER
        self.start_engine(Store(path))
        self.assertEqual(self.mode("/My Drive/b.txt"), 0o600)
        self.sync()
        self.assertEqual(self.mode("/My Drive/b.txt"), 0o400)

    def test_upgrade_runs_only_once(self):
        path = self.old_database()
        Store(path).conn.close()
        store = Store(path)
        self.assertEqual(store.count("WHERE name='My Drive'"), 1)
        self.assertEqual(store.count("WHERE name='Shared drives'"), 1)

    def test_old_my_drive_folder_named_my_drive_survives_upgrade(self):
        path = self.old_database()
        c = sqlite3.connect(path)
        c.execute("UPDATE nodes SET name='My Drive' WHERE ino=2")
        c.commit()
        c.close()
        self.start_engine(Store(path))
        self.assertEqual(self.ls("/My Drive"), ["My Drive", "b.txt"])
        self.assertEqual(self.ls("/My Drive/My Drive"), ["a.txt"])


if __name__ == "__main__":
    unittest.main()
