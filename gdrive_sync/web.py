"""Local web UI served on http://127.0.0.1:<port> only."""

import hmac
import json
import logging
import os
import secrets
import signal
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for

from . import __version__
from .auth import AuthError
from .config import CONFIG_DIR, DATA_DIR, SECRET_KEY_FILE, SETUP_TOKEN_FILE, read_or_create_secret
from .security import MIN_PASSWORD_LENGTH, LoginLimiter, hash_password, verify_password

log = logging.getLogger(__name__)

PUBLIC_ENDPOINTS = {"setup", "login", "static"}
NAUTILUS_PYTHON = Path("/usr/lib/x86_64-linux-gnu/nautilus/extensions-4/libnautilus-python.so")
NAUTILUS_EXTENSION = Path.home() / ".local/share/nautilus-python/extensions/gdrive_sync_nautilus.py"


def restart_service():
    """Have systemd restart us with the new settings (plain exit when run by hand)."""

    def restart():
        if os.environ.get("INVOCATION_ID"):  # running under systemd
            subprocess.Popen(["systemctl", "--user", "restart", "gdrive-sync.service"], start_new_session=True)
        else:
            os.kill(os.getpid(), signal.SIGTERM)

    threading.Timer(1.0, restart).start()


def file_manager_integration():
    if not NAUTILUS_EXTENSION.exists():
        return "missing-extension"
    if not any(NAUTILUS_PYTHON.parent.glob("libnautilus-python*.so")):
        return "missing-package"
    return "ok"


def create_app(config, auth, engine, restart=restart_service):
    app = Flask(__name__)
    app.secret_key = read_or_create_secret(SECRET_KEY_FILE)
    app.config.update(
        SESSION_COOKIE_NAME="gdsync_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    port = config.port
    canonical = f"127.0.0.1:{port}"
    allowed_hosts = {canonical, f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    redirect_uri = f"http://{canonical}/oauth2/callback"
    limiter = LoginLimiter()

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    app.jinja_env.globals.update(csrf_token=csrf_token, version=__version__)

    def wants_json():
        return request.path.startswith("/api/")

    @app.before_request
    def guard():
        # DNS-rebinding protection: only answer to our own loopback host names
        if request.host not in allowed_hosts:
            abort(400)
        if request.host != canonical and request.method == "GET":
            return redirect(f"http://{canonical}{request.full_path.rstrip('?')}")
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("Origin")
            if origin and origin not in allowed_origins:
                abort(403)
            sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
            if not session.get("csrf") or not hmac.compare_digest(sent, session["csrf"]):
                abort(403, "CSRF check failed. Reload the page and try again.")
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if not config.get("ui_password_hash"):
            return redirect(url_for("setup"))
        if not session.get("user"):
            if wants_json():
                return jsonify(error="login required"), 401
            return redirect(url_for("login"))
        return None

    @app.after_request
    def security_headers(resp):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; form-action 'self' https://accounts.google.com; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        # not "no-referrer": with that policy browsers send "Origin: null" on our own form
        # posts, which the Origin check above (rightly) rejects
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def start_session():
        session.clear()
        session.permanent = True
        session["user"] = True
        session["csrf"] = secrets.token_urlsafe(32)

    # ------------------------------------------------------- local login
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if config.get("ui_password_hash"):
            return redirect(url_for("login"))
        token = request.values.get("token", "")
        try:
            expected = SETUP_TOKEN_FILE.read_text().strip()
        except FileNotFoundError:
            expected = ""
        token_ok = bool(expected) and hmac.compare_digest(token, expected)
        if request.method == "POST":
            pw, confirm = request.form.get("password", ""), request.form.get("confirm", "")
            if not token_ok:
                flash("This setup link is invalid. Run 'gdrive-sync open' to get a fresh one.", "error")
            elif len(pw) < MIN_PASSWORD_LENGTH:
                flash(f"Use at least {MIN_PASSWORD_LENGTH} characters.", "error")
            elif pw != confirm:
                flash("The passwords do not match.", "error")
            else:
                config.update(ui_password_hash=hash_password(pw))
                SETUP_TOKEN_FILE.unlink(missing_ok=True)
                start_session()
                return redirect(url_for("index"))
        return render_template("setup.html", token=token, token_ok=token_ok, min_len=MIN_PASSWORD_LENGTH)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not config.get("ui_password_hash"):
            return redirect(url_for("setup"))
        if request.method == "POST":
            wait = limiter.wait_seconds()
            if wait:
                flash(f"Too many failed attempts. Try again in {wait} seconds.", "error")
            elif verify_password(request.form.get("password", ""), config.get("ui_password_hash")):
                limiter.succeeded()
                start_session()
                return redirect(url_for("index"))
            else:
                limiter.failed()
                log.warning("Failed UI login attempt")
                flash("Wrong password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.post("/password")
    def change_password():
        current, new = request.form.get("current", ""), request.form.get("new", "")
        if not verify_password(current, config.get("ui_password_hash")):
            flash("Current password is wrong.", "error")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(f"Use at least {MIN_PASSWORD_LENGTH} characters.", "error")
        else:
            config.update(ui_password_hash=hash_password(new))
            flash("Password changed.", "ok")
        return redirect(url_for("index"))

    # ----------------------------------------------------------- dashboard
    @app.get("/")
    def index():
        return render_template(
            "dashboard.html",
            has_client=auth.has_client,
            connected=auth.is_connected(),
            auth_error=auth.last_error,
            client_id=config.get("client_id") or "",
            sync_dir=str(config.sync_dir),
            poll_interval=config.get("poll_interval"),
            cache_limit_gb=config.get("cache_limit_gb"),
            keep_all_offline=engine.store.meta("keep_all_offline") == "1",
            integration=file_manager_integration(),
            redirect_uri=redirect_uri,
            token_backend=auth.store.backend,
        )

    # -------------------------------------------------------- Google auth
    @app.post("/google/client")
    def google_client():
        client_id = request.form.get("client_id", "").strip()
        client_secret = request.form.get("client_secret", "").strip()
        upload = request.files.get("client_json")
        if upload and upload.filename:
            try:
                data = json.loads(upload.read(32 * 1024))
                section = data.get("installed") or data.get("web") or {}
                client_id, client_secret = section.get("client_id", ""), section.get("client_secret", "")
                if "installed" not in data:
                    flash("That JSON is not for a 'Desktop app' client. Create a Desktop app OAuth client.", "error")
                    return redirect(url_for("index"))
            except (ValueError, AttributeError):
                flash("Could not read that file. Upload the client_secret_*.json downloaded from Google Cloud.", "error")
                return redirect(url_for("index"))
        if not client_id.endswith(".apps.googleusercontent.com") or not client_secret:
            flash("Enter a valid OAuth client ID (…apps.googleusercontent.com) and client secret.", "error")
            return redirect(url_for("index"))
        if auth.is_connected():
            auth.disconnect()
            engine.request_reset()
        config.update(client_id=client_id, client_secret=client_secret)
        flash("OAuth client saved. Now connect your Google account.", "ok")
        return redirect(url_for("index"))

    @app.post("/google/connect")
    def google_connect():
        try:
            return redirect(auth.start_flow(redirect_uri))
        except AuthError as e:
            flash(str(e), "error")
            return redirect(url_for("index"))

    @app.get("/oauth2/callback")
    def oauth_callback():
        if request.args.get("error"):
            flash(f"Google sign-in was cancelled ({request.args['error']}).", "error")
            return redirect(url_for("index"))
        try:
            auth.finish_flow(request.args.get("state", ""), request.args.get("code", ""))
        except AuthError as e:
            flash(str(e), "error")
            return redirect(url_for("index"))
        flash("Google account connected. Initial sync is starting.", "ok")
        engine.sync_now()
        return redirect(url_for("index"))

    @app.post("/google/disconnect")
    def google_disconnect():
        auth.disconnect()
        engine.request_reset()
        flash("Disconnected and access revoked. Your local files were left in place.", "ok")
        return redirect(url_for("index"))

    @app.post("/settings")
    def settings():
        try:
            poll = int(request.form.get("poll_interval", config.get("poll_interval")))
            cache_gb = float(request.form.get("cache_limit_gb", config.get("cache_limit_gb")))
        except ValueError:
            poll, cache_gb = 0, -1
        if not 10 <= poll <= 3600:
            flash("Check interval must be between 10 and 3600 seconds.", "error")
            return redirect(url_for("index"))
        if not 0.1 <= cache_gb <= 100_000:
            flash("Space for recently opened files must be between 0.1 and 100000 GB.", "error")
            return redirect(url_for("index"))
        updates = {"poll_interval": poll, "cache_limit_gb": cache_gb}
        keep_all = request.form.get("keep_all_offline") == "on"
        if keep_all != (engine.store.meta("keep_all_offline") == "1"):
            engine.set_keep_all_offline(keep_all)
            flash("All files will be downloaded and kept on this computer." if keep_all
                  else "Files will be downloaded only when you open them.", "ok")
        new_dir = request.form.get("sync_dir", "").strip()
        move = False
        if new_dir and Path(new_dir).expanduser() != config.sync_dir:
            path = Path(new_dir).expanduser()
            forbidden = {Path("/"), Path.home(), CONFIG_DIR, DATA_DIR}
            if not path.is_absolute() or path.resolve() in {p.resolve() for p in forbidden}:
                flash("Choose a dedicated absolute folder (not / or your home folder).", "error")
                return redirect(url_for("index"))
            if path.exists() and (not path.is_dir() or any(path.iterdir())):
                flash("Choose a folder that is empty or does not exist yet.", "error")
                return redirect(url_for("index"))
            updates["sync_dir"] = str(path)
            move = True
        config.update(**updates)
        if move:
            flash(f"Moving your Drive folder to {updates['sync_dir']}. The service restarts; reload in a few seconds.", "ok")
            restart()
        engine.sync_now()
        return redirect(url_for("index"))

    # ----------------------------------------------------------------- API
    @app.get("/api/status")
    def api_status():
        st = engine.status()
        st.update(
            connected=auth.is_connected(),
            has_client=auth.has_client,
            paused=bool(config.get("paused")),
            auth_error=auth.last_error,
        )
        return jsonify(st)

    @app.post("/api/pause")
    def api_pause():
        config.update(paused=True)
        return jsonify(ok=True)

    @app.post("/api/resume")
    def api_resume():
        config.update(paused=False)
        engine.sync_now()
        return jsonify(ok=True)

    @app.post("/api/sync-now")
    def api_sync_now():
        engine.sync_now()
        return jsonify(ok=True)

    @app.post("/api/confirm-deletes")
    def api_confirm_deletes():
        engine.confirm_mass_delete()
        return jsonify(ok=True)

    @app.post("/api/restore-deletes")
    def api_restore_deletes():
        engine.restore_deleted()
        return jsonify(ok=True)

    @app.post("/api/resync")
    def api_resync():
        engine.request_reset()
        return jsonify(ok=True)

    @app.errorhandler(400)
    @app.errorhandler(403)
    def forbidden(e):
        if wants_json():
            return jsonify(error=e.description), e.code
        return render_template("error.html", message=e.description, code=e.code), e.code

    return app
