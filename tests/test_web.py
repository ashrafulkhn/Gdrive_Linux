"""Security behaviour of the local web UI."""

import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if "XDG_CONFIG_HOME" not in os.environ or not os.environ["XDG_CONFIG_HOME"].startswith(tempfile.gettempdir()):
    _tmp_home = tempfile.mkdtemp()
    os.environ["XDG_CONFIG_HOME"] = os.path.join(_tmp_home, "config")
    os.environ["XDG_DATA_HOME"] = os.path.join(_tmp_home, "data")

from gdrive_sync import security  # noqa: E402
from gdrive_sync.auth import GoogleAuth  # noqa: E402
from gdrive_sync.config import SETUP_TOKEN_FILE, Config, ensure_dirs, read_or_create_secret  # noqa: E402
from gdrive_sync.web import create_app  # noqa: E402

BASE = "http://127.0.0.1:8642"


class MemStore:
    backend = "memory"

    def __init__(self):
        self.token = None

    def load(self):
        return self.token

    def save(self, t):
        self.token = t

    def clear(self):
        self.token = None


class FakeStore:
    def meta(self, key, default=None):
        return default


class FakeEngine:
    store = FakeStore()

    def status(self):
        return {"state": "idle", "activity": [], "conflicts": [], "files_tracked": 0, "sync_dir": "/x"}

    def sync_now(self):
        pass

    def request_reset(self):
        pass

    def confirm_mass_delete(self):
        pass


class WebTestCase(unittest.TestCase):
    def setUp(self):
        ensure_dirs()
        SETUP_TOKEN_FILE.unlink(missing_ok=True)
        self.token = read_or_create_secret(SETUP_TOKEN_FILE)
        self.config = Config(Path(tempfile.mkdtemp()) / "config.json")
        self.config.update(port=8642)
        self.auth = GoogleAuth(self.config, store=MemStore())
        self.restarts = []
        self.app = create_app(self.config, self.auth, FakeEngine(), restart=lambda: self.restarts.append(1))
        self.client = self.app.test_client()

    def csrf(self, client=None):
        client = client or self.client
        client.get(f"{BASE}/login", follow_redirects=True)
        with client.session_transaction("/", base_url=BASE) as s:
            return s.get("csrf")

    def do_setup(self, password="correct horse battery"):
        self.client.get(f"{BASE}/setup?token={self.token}")
        return self.client.post(
            f"{BASE}/setup",
            data={"csrf": self.csrf(), "token": self.token, "password": password, "confirm": password},
        )

    def test_rejects_foreign_host_header(self):
        self.assertEqual(self.client.get("http://evil.example:8642/").status_code, 400)

    def test_first_run_redirects_to_setup_and_needs_token(self):
        r = self.client.get(f"{BASE}/")
        self.assertIn("/setup", r.headers["Location"])
        self.client.get(f"{BASE}/setup")
        r = self.client.post(
            f"{BASE}/setup", data={"csrf": self.csrf(), "token": "wrong", "password": "x" * 12, "confirm": "x" * 12}
        )
        self.assertIsNone(self.config.get("ui_password_hash"))

    def test_setup_then_dashboard(self):
        r = self.do_setup()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.config.get("ui_password_hash").startswith("scrypt$"))
        self.assertFalse(SETUP_TOKEN_FILE.exists())  # one-time token consumed
        self.assertEqual(self.client.get(f"{BASE}/").status_code, 200)

    def test_post_without_csrf_is_rejected(self):
        self.do_setup()
        self.assertEqual(self.client.post(f"{BASE}/api/pause").status_code, 403)
        self.assertFalse(self.config.get("paused"))

    def test_cross_origin_post_is_rejected(self):
        self.do_setup()
        token = self.csrf()
        r = self.client.post(f"{BASE}/api/pause", headers={"X-CSRF-Token": token, "Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = self.client.post(f"{BASE}/api/pause", headers={"X-CSRF-Token": token, "Origin": BASE})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.config.get("paused"))

    def test_browser_form_posts_carry_a_real_origin(self):
        # Browsers send "Origin: null" on form posts from pages with Referrer-Policy: no-referrer,
        # which would make every form fail the Origin check.
        r = self.client.get(f"{BASE}/setup?token={self.token}")
        self.assertEqual(r.headers["Referrer-Policy"], "same-origin")
        r = self.client.post(
            f"{BASE}/setup",
            headers={"Origin": BASE},
            data={"csrf": self.csrf(), "token": self.token, "password": "x" * 12, "confirm": "x" * 12},
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.config.get("ui_password_hash"))

    def test_null_origin_is_rejected(self):
        self.do_setup()
        r = self.client.post(f"{BASE}/api/pause", headers={"X-CSRF-Token": self.csrf(), "Origin": "null"})
        self.assertEqual(r.status_code, 403)

    def test_api_requires_login(self):
        self.do_setup()
        other = self.app.test_client()
        self.assertEqual(other.get(f"{BASE}/api/status").status_code, 401)

    def test_login_lockout(self):
        self.do_setup()
        other = self.app.test_client()
        for _ in range(security.LoginLimiter().free_attempts):
            other.post(f"{BASE}/login", data={"csrf": self.csrf(other), "password": "nope"})
        r = other.post(f"{BASE}/login", data={"csrf": self.csrf(other), "password": "correct horse battery"})
        self.assertIn(b"Too many failed attempts", r.data)

    def test_security_headers(self):
        r = self.client.get(f"{BASE}/setup")
        self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertIn("HttpOnly", r.headers.get("Set-Cookie", ""))

    def test_settings_validate_and_move_folder_restarts(self):
        self.do_setup()
        token = self.csrf()
        r = self.client.post(f"{BASE}/settings", data={"csrf": token, "poll_interval": "5", "cache_limit_gb": "10"})
        self.assertEqual(self.config.get("poll_interval"), 30)
        target = Path(tempfile.mkdtemp()) / "Drive"
        self.client.post(f"{BASE}/settings", data={"csrf": token, "poll_interval": "60", "cache_limit_gb": "2.5",
                                                   "sync_dir": str(target)})
        self.assertEqual((self.config.get("poll_interval"), self.config.get("cache_limit_gb")), (60, 2.5))
        self.assertEqual(self.config.get("sync_dir"), str(target))
        self.assertEqual(self.restarts, [1])
        self.client.post(f"{BASE}/settings", data={"csrf": token, "poll_interval": "60", "cache_limit_gb": "2.5",
                                                   "sync_dir": str(Path.home())})
        self.assertEqual(self.config.get("sync_dir"), str(target))

    def test_oauth_uses_pkce_and_checks_state(self):
        self.do_setup()
        self.config.update(client_id="abc.apps.googleusercontent.com", client_secret="s")
        r = self.client.post(f"{BASE}/google/connect", data={"csrf": self.csrf()})
        q = parse_qs(urlparse(r.headers["Location"]).query)
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["redirect_uri"], [f"{BASE}/oauth2/callback"])
        self.assertTrue(q["state"][0])
        self.client.get(f"{BASE}/oauth2/callback?state=forged&code=x")
        self.assertFalse(self.auth.is_connected())


if __name__ == "__main__":
    unittest.main()
