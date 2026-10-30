"""In-memory stand-ins for Google Drive, the config and auth, used by the tests."""

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gdrive_sync.config import DEFAULTS
from gdrive_sync.drive import FOLDER_MIME, SHORTCUT_MIME, NotFound

DOC_MIME = "application/vnd.google-apps.document"


class FakeDrive:
    def __init__(self):
        self.root = "root"
        self.files = {}
        self.log = []
        self.n = 0
        self.clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.downloads = 0
        self.ranges = 0
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
        if mime.startswith("application/vnd.google-apps."):
            if mime != FOLDER_MIME:
                f["webViewLink"] = f"https://docs.google.com/document/d/{fid}/edit"
        else:
            f.update(content=content, md5Checksum=hashlib.md5(content).hexdigest(), size=str(len(content)))
        self.files[fid] = f
        self._touch(fid)
        return fid

    def r_folder(self, name, parent=None):
        return self.r_create(name, parent, mime=FOLDER_MIME)

    def r_modify(self, fid, content):
        self.files[fid].update(content=content, md5Checksum=hashlib.md5(content).hexdigest(), size=str(len(content)))
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

    def live(self):
        return [f for f in self.files.values() if not f["trashed"]]

    # ---- the API used by the engine
    def _meta(self, fid):
        f = dict(self.files[fid])
        f.pop("content", None)
        f["parents"] = list(f["parents"])
        return f

    def about(self):
        return {"user": {"emailAddress": "me@example.com"}, "storageQuota": {"usage": "1000", "limit": "100000"}}

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

    def upload_new(self, path, name, parent_id, mime=None):
        self.uploads += 1
        return self._meta(self.r_create(name, parent_id, Path(path).read_bytes()))

    def upload_update(self, fid, path, mime=None):
        self.uploads += 1
        self.r_modify(fid, Path(path).read_bytes())
        return self._meta(fid)

    def download(self, fid, dest, expected_md5=None):
        self.downloads += 1
        data = self.files[fid]["content"]
        Path(dest).write_bytes(data)
        return hashlib.md5(data).hexdigest()

    def download_range(self, fid, start, end):
        self.ranges += 1
        return self.files[fid]["content"][start : end + 1]


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
