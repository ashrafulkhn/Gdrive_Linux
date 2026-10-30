"""GDrive Sync integration for GNOME Files (Nautilus 43+).

Shows a sync-status emblem on every item in the Drive folder, adds a
"Drive status" list column, and adds right-click actions:
Make available offline / Free up space / Download now / Open in Google Drive.

Status is read from extended attributes served by the gdrive-sync
filesystem, so this extension never touches the network itself.
Requires: sudo apt install python3-nautilus   (then: nautilus -q)
"""

import json
import os
from collections import OrderedDict

import gi

gi.require_version("Nautilus", "4.0")
from gi.repository import Gio, GLib, GObject, Nautilus  # noqa: E402

STATUS = "user.gdrive.status"
LINK = "user.gdrive.link"
GENERATION = "user.gdrive.generation"
ACTION = "user.gdrive.action"
EMBLEMS = {
    "cloud": "gdrive-cloud",
    "local": "gdrive-local",
    "pinned": "gdrive-pinned",
    "syncing": "gdrive-syncing",
    "error": "gdrive-error",
}
LABELS = {
    "cloud": "Online only",
    "local": "On this computer",
    "pinned": "Always available offline",
    "syncing": "Syncing…",
    "error": "Sync problem",
}
MAX_WATCHED = 4000


def drive_root():
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(config_home, "gdrive-sync", "config.json")) as f:
            path = json.load(f).get("sync_dir")
    except (OSError, ValueError):
        path = None
    return os.path.realpath(os.path.expanduser(path or "~/GoogleDrive"))


def xattr(path, name):
    try:
        return os.getxattr(path, name).decode()
    except OSError:
        return None


class GDriveSyncExtension(GObject.GObject, Nautilus.InfoProvider, Nautilus.MenuProvider, Nautilus.ColumnProvider):
    def __init__(self):
        super().__init__()
        self.root = drive_root()
        self.watched = OrderedDict()  # path -> (file info, last status)
        self.generation = None
        GLib.timeout_add(1500, self._poll)

    # ------------------------------------------------------------ helpers
    def _path(self, file):
        if file.get_uri_scheme() != "file":
            return None
        location = file.get_location()
        path = location.get_path() if location else None
        if path and (path == self.root or path.startswith(self.root + os.sep)):
            return path
        return None

    def _poll(self):
        gen = xattr(self.root, GENERATION)
        if gen is not None and gen != self.generation:
            self.generation = gen
            for path, (file, old) in list(self.watched.items()):
                new = xattr(path, STATUS)
                if new is None:
                    self.watched.pop(path, None)
                elif new != old:
                    file.invalidate_extension_info()  # Nautilus calls update_file_info again
        return True  # keep polling

    # --------------------------------------------------------------- icons
    def update_file_info(self, file):
        path = self._path(file)
        if path is None:
            return
        status = xattr(path, STATUS)
        if status is None:
            return
        if status in EMBLEMS:
            file.add_emblem(EMBLEMS[status])
        file.add_string_attribute("gdrive_status", LABELS.get(status, status))
        self.watched[path] = (file, status)
        self.watched.move_to_end(path)
        while len(self.watched) > MAX_WATCHED:
            self.watched.popitem(last=False)

    def get_columns(self):
        return [
            Nautilus.Column(
                name="GDriveSync::status",
                attribute="gdrive_status",
                label="Drive status",
                description="Whether the file is on this computer or only in Google Drive",
            )
        ]

    # ---------------------------------------------------------------- menu
    def get_file_items(self, files):
        paths = [p for p in (self._path(f) for f in files) if p and xattr(p, STATUS) is not None]
        if not paths:
            return []
        statuses = {xattr(p, STATUS) for p in paths}
        items = []
        if statuses - {"pinned"}:
            items.append(self._item("pin", "Make available offline", "Download and always keep on this computer", paths))
        if statuses & {"local", "pinned"} or any(os.path.isdir(p) for p in paths):
            items.append(self._item("free", "Free up space", "Keep online only; downloads again when opened", paths))
        if "cloud" in statuses:
            items.append(self._item("download", "Download now", "Download once without keeping it permanently", paths))
        if len(paths) == 1:
            item = Nautilus.MenuItem(name="GDriveSync::open_web", label="Open in Google Drive", tip="Open in your browser")
            item.connect("activate", self._open_web, paths[0])
            items.append(item)
        return items

    def get_background_items(self, folder):
        return self.get_file_items([folder])

    def _item(self, action, label, tip, paths):
        item = Nautilus.MenuItem(name=f"GDriveSync::{action}", label=label, tip=tip)
        item.connect("activate", self._run_action, action, paths)
        return item

    def _run_action(self, _item, action, paths):
        for path in paths:
            try:
                os.setxattr(path, ACTION, action.encode())
            except OSError as e:
                print(f"gdrive-sync: {action} failed for {path}: {e}")

    def _open_web(self, _item, path):
        url = xattr(path, LINK)
        if url and url.startswith("https://"):
            Gio.AppInfo.launch_default_for_uri(url, None)
