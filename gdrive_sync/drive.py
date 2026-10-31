"""Minimal Google Drive v3 REST client with retries, resumable uploads and
checksum-verified downloads."""

import hashlib
import json
import logging
import mimetypes
import os
import random
import secrets
import time

import requests

from .auth import AuthError

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
CAPABILITIES = "canEdit,canAddChildren,canRename,canTrash,canMoveItemWithinDrive,canDownload"
FILE_FIELDS = f"id,name,mimeType,parents,md5Checksum,size,modifiedTime,trashed,webViewLink,capabilities({CAPABILITIES})"

SIMPLE_UPLOAD_LIMIT = 5 * 1024 * 1024
CHUNK_SIZE = 32 * 256 * 1024  # 8 MiB, must be a multiple of 256 KiB
MAX_RETRIES = 6
TIMEOUT = (15, 120)
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
DENIED_REASONS = {"insufficientFilePermissions", "insufficientParentPermissions"}


def guess_mime(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class DriveError(Exception):
    def __init__(self, message, status=None, reason=None):
        super().__init__(message)
        self.status = status
        self.reason = reason


class NotFound(DriveError):
    pass


def is_denied(e):
    """Google refused the action because of your role on the file (retrying can't help)."""
    return isinstance(e, DriveError) and e.status == 403 and e.reason in DENIED_REASONS


def _reason(resp):
    try:
        err = resp.json().get("error", {})
        errors = err.get("errors") or [{}]
        return errors[0].get("reason"), err.get("message") or resp.reason
    except ValueError:
        return None, resp.reason


class DriveClient:
    def __init__(self, auth):
        self.auth = auth
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "gdrive-sync-linux/1.0"

    # -- transport ----------------------------------------------------------
    def _request(self, method, url, *, ok=(200,), headers=None, data_factory=None, stream=False, **kw):
        if "/files" in url or "/changes" in url:  # without this, shared drive items are invisible
            kw["params"] = {**kw.get("params", {}), "supportsAllDrives": "true"}
        refreshed = False
        for attempt in range(MAX_RETRIES):
            hdrs = dict(headers or {})
            hdrs["Authorization"] = f"Bearer {self.auth.access_token(force_refresh=refreshed)}"
            if data_factory is not None:
                kw["data"] = data_factory()
            try:
                resp = self.session.request(method, url, headers=hdrs, timeout=TIMEOUT, stream=stream, **kw)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == MAX_RETRIES - 1:
                    raise
                self._sleep(attempt, f"network error: {e}")
                continue
            if resp.status_code in ok:
                return resp
            reason, message = _reason(resp)
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                continue
            if resp.status_code == 401:
                raise AuthError("Google rejected the access token")
            if resp.status_code in (429, 500, 502, 503, 504) or (resp.status_code == 403 and reason in RATE_REASONS):
                self._sleep(attempt, f"HTTP {resp.status_code} {reason or ''}")
                continue
            if resp.status_code == 404:
                raise NotFound(message, 404, reason)
            raise DriveError(f"{method} {url.split('?')[0]} failed: HTTP {resp.status_code} {message}", resp.status_code, reason)
        raise DriveError(f"{method} {url.split('?')[0]} failed after {MAX_RETRIES} attempts")

    @staticmethod
    def _sleep(attempt, why):
        delay = min(60, 2 ** attempt) + random.random()
        log.info("Retrying in %.1fs (%s)", delay, why)
        time.sleep(delay)

    # -- metadata -------------------------------------------------------------
    def about(self):
        return self._request("GET", f"{API}/about", params={"fields": "user(emailAddress,displayName),storageQuota"}).json()

    def root_id(self):
        return self._request("GET", f"{API}/files/root", params={"fields": "id"}).json()["id"]

    def get(self, file_id):
        return self._request("GET", f"{API}/files/{file_id}", params={"fields": FILE_FIELDS}).json()

    def list_drives(self):
        """The shared drives the user is a member of."""
        drives, token = [], None
        while True:
            params = {"pageSize": 100, "fields": "nextPageToken,drives(id,name,capabilities(canAddChildren))"}
            if token:
                params["pageToken"] = token
            body = self._request("GET", f"{API}/drives", params=params).json()
            drives.extend(body.get("drives", []))
            token = body.get("nextPageToken")
            if not token:
                return drives

    def list_all(self, drive_id=None):
        """All non-trashed files in one shared drive, or those visible to the user
        outside shared drives (filtered to My Drive by the caller)."""
        files, token = [], None
        while True:
            params = {
                "q": "trashed = false",
                "fields": f"nextPageToken,files({FILE_FIELDS})",
                "pageSize": 1000,
                "spaces": "drive",
            }
            if drive_id:
                params.update(corpora="drive", driveId=drive_id, includeItemsFromAllDrives="true")
            if token:
                params["pageToken"] = token
            body = self._request("GET", f"{API}/files", params=params).json()
            files.extend(body.get("files", []))
            token = body.get("nextPageToken")
            if not token:
                return files

    def start_page_token(self, drive_id=None):
        params = {"driveId": drive_id} if drive_id else {}
        return self._request("GET", f"{API}/changes/startPageToken", params=params).json()["startPageToken"]

    def changes(self, page_token, drive_id=None):
        """Return (changes, new_start_page_token) since ``page_token``, for My Drive or one shared drive."""
        out = []
        scope = {"driveId": drive_id, "includeItemsFromAllDrives": "true"} if drive_id else {"restrictToMyDrive": "true"}
        while True:
            body = self._request(
                "GET",
                f"{API}/changes",
                params={
                    **scope,
                    "pageToken": page_token,
                    "pageSize": 1000,
                    "includeRemoved": "true",
                    "spaces": "drive",
                    "fields": f"nextPageToken,newStartPageToken,changes(changeType,fileId,removed,file({FILE_FIELDS}))",
                },
            ).json()
            out.extend(body.get("changes", []))
            if "newStartPageToken" in body:
                return out, body["newStartPageToken"]
            page_token = body["nextPageToken"]

    def create_folder(self, name, parent_id):
        return self._request(
            "POST",
            f"{API}/files",
            params={"fields": FILE_FIELDS},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        ).json()

    def update_metadata(self, file_id, name=None, add_parent=None, remove_parent=None):
        params = {"fields": FILE_FIELDS}
        if add_parent:
            params["addParents"] = add_parent
        if remove_parent:
            params["removeParents"] = remove_parent
        body = {"name": name} if name else {}
        return self._request("PATCH", f"{API}/files/{file_id}", params=params, json=body).json()

    def trash(self, file_id):
        self._request("PATCH", f"{API}/files/{file_id}", params={"fields": "id"}, json={"trashed": True})

    # -- content ------------------------------------------------------------
    def upload_new(self, local_path, name, parent_id, mime=None):
        return self._upload("POST", None, local_path, {"name": name, "parents": [parent_id]}, mime or guess_mime(name))

    def upload_update(self, file_id, local_path, mime=None):
        return self._upload("PATCH", file_id, local_path, {}, mime or guess_mime(str(local_path)))

    def _upload(self, method, file_id, local_path, metadata, mime):
        size = os.path.getsize(local_path)
        target = f"{UPLOAD_API}/files" + (f"/{file_id}" if file_id else "")
        if size <= SIMPLE_UPLOAD_LIMIT:
            boundary = "gdsync" + secrets.token_hex(12)

            def body():
                with open(local_path, "rb") as f:
                    content = f.read()
                return (
                    f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                    + json.dumps(metadata).encode()
                    + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
                    + content
                    + f"\r\n--{boundary}--".encode()
                )

            return self._request(
                method,
                target,
                params={"uploadType": "multipart", "fields": FILE_FIELDS},
                headers={"Content-Type": f"multipart/related; boundary={boundary}"},
                data_factory=body,
            ).json()

        session_url = self._request(
            method,
            target,
            params={"uploadType": "resumable", "fields": FILE_FIELDS},
            headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
            json=metadata,
        ).headers["Location"]
        offset, failures = 0, 0
        with open(local_path, "rb") as f:
            while True:
                f.seek(offset)
                chunk = f.read(CHUNK_SIZE)
                end = offset + len(chunk) - 1
                try:
                    resp = self.session.put(
                        session_url,
                        data=chunk,
                        headers={"Content-Range": f"bytes {offset}-{end}/{size}"},
                        timeout=TIMEOUT,
                    )
                except (requests.ConnectionError, requests.Timeout):
                    resp = None
                if resp is not None and resp.status_code in (200, 201):
                    return resp.json()
                if resp is not None and resp.status_code == 308:
                    rng = resp.headers.get("Range")
                    offset = int(rng.split("-")[1]) + 1 if rng else 0
                    failures = 0
                    continue
                if resp is not None and resp.status_code not in (429, 500, 502, 503, 504):
                    raise DriveError(f"Upload failed: HTTP {resp.status_code} {_reason(resp)[1]}", resp.status_code)
                failures += 1
                if failures >= MAX_RETRIES:
                    raise DriveError("Upload failed after repeated errors")
                self._sleep(failures, "resumable upload chunk failed")
                # ask the server how much it already has
                try:
                    st = self.session.put(session_url, headers={"Content-Range": f"bytes */{size}"}, timeout=TIMEOUT)
                    if st.status_code in (200, 201):
                        return st.json()
                    rng = st.headers.get("Range")
                    offset = int(rng.split("-")[1]) + 1 if rng else 0
                except requests.RequestException:
                    pass

    def download_range(self, file_id, start, end):
        """Return bytes ``start..end`` (inclusive) of a file without downloading all of it."""
        resp = self._request(
            "GET",
            f"{API}/files/{file_id}",
            ok=(200, 206),
            params={"alt": "media"},
            headers={"Range": f"bytes={start}-{end}"},
            stream=True,
        )
        with resp:
            data = b""
            for chunk in resp.iter_content(64 * 1024):
                data += chunk
                if len(data) > end - start:
                    break
        return data[: end - start + 1]

    def download(self, file_id, dest, expected_md5=None):
        """Stream file content to ``dest`` and return its md5 hex digest."""
        resp = self._request("GET", f"{API}/files/{file_id}", params={"alt": "media"}, stream=True)
        md5 = hashlib.md5()
        with resp, open(dest, "wb") as f:
            for chunk in resp.iter_content(1024 * 1024):
                md5.update(chunk)
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        digest = md5.hexdigest()
        if expected_md5 and digest != expected_md5:
            raise DriveError(f"Checksum mismatch downloading {file_id}")
        return digest
