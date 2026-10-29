"""Google OAuth 2.0 (installed-app loopback flow with PKCE) and token storage.

Only the refresh token is persisted, in the desktop keyring (GNOME Keyring /
Secret Service) when available, otherwise in a 0600 file. Access tokens live
in memory only.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from urllib.parse import urlencode

import requests

from .config import TOKEN_FILE, write_private

log = logging.getLogger(__name__)

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
# Full Drive access is required for two-way sync of existing files;
# drive.file would only cover files created by this app.
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
FLOW_TTL = 600

KEYRING_SERVICE = "gdrive-sync"
KEYRING_USER = "refresh_token"


class AuthError(Exception):
    """Raised when there is no usable Google authorization."""


class TokenStore:
    def __init__(self):
        self._keyring = None
        try:
            import keyring
            from keyring.backends import fail

            if not isinstance(keyring.get_keyring(), fail.Keyring):
                self._keyring = keyring
        except Exception:  # keyring missing or no D-Bus session
            self._keyring = None
        if self._keyring is None:
            log.warning("No system keyring available; storing token in %s (mode 0600)", TOKEN_FILE)

    @property
    def backend(self):
        return "system keyring" if self._keyring else f"file ({TOKEN_FILE})"

    def load(self):
        if self._keyring:
            return self._keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        try:
            return json.loads(TOKEN_FILE.read_text()).get("refresh_token")
        except FileNotFoundError:
            return None

    def save(self, refresh_token):
        if self._keyring:
            self._keyring.set_password(KEYRING_SERVICE, KEYRING_USER, refresh_token)
        else:
            write_private(TOKEN_FILE, json.dumps({"refresh_token": refresh_token}))

    def clear(self):
        if self._keyring:
            try:
                self._keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
            except Exception:
                pass
        TOKEN_FILE.unlink(missing_ok=True)


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class GoogleAuth:
    def __init__(self, config, store=None):
        self.config = config
        self.store = store or TokenStore()
        self._lock = threading.Lock()
        self._pending = None
        self._access_token = None
        self._expiry = 0.0
        self._refresh_token = None
        self.last_error = None
        try:
            self._refresh_token = self.store.load()
        except Exception as e:  # keyring may be locked right after boot
            log.warning("Could not read stored token yet: %s", e)

    # -- state -------------------------------------------------------------
    @property
    def has_client(self):
        return bool(self.config.get("client_id") and self.config.get("client_secret"))

    def is_connected(self):
        if self._refresh_token is None:
            try:
                self._refresh_token = self.store.load()
            except Exception:
                return False
        return bool(self._refresh_token)

    # -- authorization flow ----------------------------------------------
    def start_flow(self, redirect_uri):
        if not self.has_client:
            raise AuthError("OAuth client is not configured")
        verifier = _b64url(secrets.token_bytes(64))
        state = secrets.token_urlsafe(32)
        with self._lock:
            self._pending = {
                "state": state,
                "verifier": verifier,
                "redirect_uri": redirect_uri,
                "created": time.time(),
            }
        params = {
            "client_id": self.config.get("client_id"),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": DRIVE_SCOPE,
            "state": state,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{AUTH_URI}?{urlencode(params)}"

    def finish_flow(self, state, code):
        with self._lock:
            pending, self._pending = self._pending, None  # single use
        if (
            not pending
            or not state
            or not hmac.compare_digest(pending["state"], state)
            or time.time() - pending["created"] > FLOW_TTL
        ):
            raise AuthError("Invalid or expired login attempt. Please try again.")
        resp = requests.post(
            TOKEN_URI,
            data={
                "code": code,
                "client_id": self.config.get("client_id"),
                "client_secret": self.config.get("client_secret"),
                "redirect_uri": pending["redirect_uri"],
                "grant_type": "authorization_code",
                "code_verifier": pending["verifier"],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise AuthError(f"Token exchange failed: {_error_text(resp)}")
        tok = resp.json()
        if DRIVE_SCOPE not in tok.get("scope", "").split():
            raise AuthError("Google Drive access was not granted. Tick the Drive permission on the consent screen.")
        if not tok.get("refresh_token"):
            raise AuthError("Google did not return a refresh token. Remove the app at myaccount.google.com/permissions and retry.")
        with self._lock:
            self._refresh_token = tok["refresh_token"]
            self._access_token = tok["access_token"]
            self._expiry = time.time() + int(tok.get("expires_in", 3600))
            self.last_error = None
        self.store.save(tok["refresh_token"])

    # -- tokens -----------------------------------------------------------
    def access_token(self, force_refresh=False):
        with self._lock:
            if not force_refresh and self._access_token and time.time() < self._expiry - 60:
                return self._access_token
            if not self.is_connected():
                raise AuthError("Not connected to Google")
            resp = requests.post(
                TOKEN_URI,
                data={
                    "client_id": self.config.get("client_id"),
                    "client_secret": self.config.get("client_secret"),
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=30,
            )
            if resp.status_code in (400, 401):
                # invalid_grant: revoked, expired (7-day limit for "Testing" apps) or password changed
                self.last_error = f"Google rejected the stored login ({_error_text(resp)}). Please reconnect."
                self._forget()
                raise AuthError(self.last_error)
            resp.raise_for_status()
            tok = resp.json()
            self._access_token = tok["access_token"]
            self._expiry = time.time() + int(tok.get("expires_in", 3600))
            return self._access_token

    def disconnect(self):
        with self._lock:
            token = self._refresh_token
            self._forget()
        if token:
            try:
                requests.post(REVOKE_URI, data={"token": token}, timeout=15)
            except requests.RequestException as e:
                log.warning("Token revocation failed: %s", e)

    def _forget(self):
        self._refresh_token = None
        self._access_token = None
        self._expiry = 0.0
        try:
            self.store.clear()
        except Exception as e:
            log.warning("Could not clear stored token: %s", e)


def _error_text(resp):
    try:
        body = resp.json()
        return body.get("error_description") or body.get("error") or resp.reason
    except ValueError:
        return f"HTTP {resp.status_code}"
