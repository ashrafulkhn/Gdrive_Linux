# GDrive Sync for Linux

A two-way Google Drive sync client for Ubuntu and other Linux desktops. It
works like Google Drive for desktop does on Windows (in "mirror" mode): a
normal folder, `~/GoogleDrive`, that stays in sync with **My Drive** in both
directions. There is a local web dashboard at **http://127.0.0.1:8642**, and
the client starts automatically when you log in.

- Edit, add, rename, move or delete files locally → the change appears in Drive within seconds (inotify).
- Changes made in Drive (web, phone, other PCs) → they appear locally within 30 s (Drive Changes API).
- Google Docs/Sheets/Slides appear as `.gdoc`/`.gsheet`/`.gslides` files. Double-clicking one opens it in your browser.
- Conflicts never lose data. If a file changed on both sides, your local version is kept beside it as `name (conflict <host> <date>).ext`.
- Deletions are recoverable. Local deletions go to the **Drive trash**, and remote deletions go to your **desktop Trash**.

## Install

```bash
git clone <this repo> && cd Gdrive_Linux
./install.sh            # or: GDRIVE_SYNC_DIR=/data/Drive ./install.sh
```

The installer runs as your normal user and needs no `sudo`. If it complains,
install `python3-venv` (`sudo apt install python3-venv`).

The installer:

1. creates a virtualenv in `~/.local/share/gdrive-sync/venv`,
2. installs a **systemd user service**, `gdrive-sync.service`, that starts with your desktop session and restarts on failure,
3. adds a "GDrive Sync" app launcher, a `.gdoc` file handler, and a "Google Drive" bookmark in Files,
4. opens the dashboard so you can finish setup.

### First-time setup (about 5 minutes)

1. **Dashboard password.** The browser opens a one-time setup link. Choose a password for the dashboard. It stops other users and programs on your computer from controlling the sync. It is not your Google password.
2. **Google API client.** Google requires every Drive app to have an OAuth client. You create your own, so no third party ever sits between you and your data:
   1. Go to [console.cloud.google.com](https://console.cloud.google.com/projectcreate) and create a project.
   2. Enable the **Google Drive API**.
   3. In **Google Auth Platform → Audience**, choose **External** and add your Gmail address as a test user. Then click **Publish app**.
      > ⚠️ If the app stays in *Testing*, Google expires the login every 7 days. Once published, Google shows an "unverified app" warning at sign-in, because only you use it. Click *Advanced → Go to …* to continue.
   4. In **Clients**, click **Create client**, choose **Desktop app**, and download the JSON.
   5. Upload that JSON in the dashboard.
3. **Sign in with Google.** Grant Drive access. The initial sync starts right away.

## Daily use

| What | How |
|---|---|
| Dashboard | App menu → **GDrive Sync**, or `gdrive-sync open` |
| Service status / logs | `systemctl --user status gdrive-sync` · `journalctl --user -u gdrive-sync -f` |
| Stop / start | `systemctl --user stop gdrive-sync` / `start` |
| Forgot dashboard password | `gdrive-sync reset-password`, then `systemctl --user restart gdrive-sync && gdrive-sync open` |
| Uninstall | `./uninstall.sh` (keeps settings) or `./uninstall.sh --purge` (also revokes and deletes the Google login) |

The dashboard shows the sync status, storage use, recent activity and
conflicts. It also has Pause/Resume, Sync now, settings (folder, check
interval), password change and Disconnect.

### Port

The UI uses port **8642**. It is easy to remember, and no common service
uses it. To change it, set `"port"` in `~/.config/gdrive-sync/config.json`,
or set `GDRIVE_SYNC_PORT` in the service environment. The OAuth redirect URI
follows the port automatically, because Desktop OAuth clients accept any
loopback port.

## Security

| Area | What is done |
|---|---|
| Network exposure | The server binds to `127.0.0.1` only, so nothing on your LAN can reach it. |
| DNS-rebinding | Requests are refused unless the `Host` is `127.0.0.1:8642` or `localhost:8642`. |
| Dashboard access | A local password (scrypt-hashed), with exponential lockout after failed attempts. The session cookie is signed, `HttpOnly`, `SameSite=Lax` and lasts 12 h. |
| First-run hijack | Setup needs a one-time token from a `0600` file that only your user can read. A different local user can't claim the dashboard first. |
| CSRF / clickjacking | Every POST needs a CSRF token and a same-origin `Origin`. The page has a strict CSP (no inline scripts), `X-Frame-Options: DENY` and `no-referrer`. |
| XSS | File names come from Drive and may be hostile. Templates auto-escape them, and the JavaScript only uses `textContent`. |
| Google login | OAuth 2.0 authorization code flow with **PKCE (S256)**, a single-use `state`, and a loopback redirect. The app never sees your Google password. |
| Token storage | Only the refresh token is stored, in **GNOME Keyring** (Secret Service). Without a keyring it goes to a `0600` file. Access tokens are kept in memory only. Disconnect **revokes** the token at Google. |
| Scope | `drive` only, with no profile/e-mail scopes. Full Drive scope is needed to sync files this app didn't create. |
| Local files | The service runs with `UMask=0077`. The sync folder is forced to `0700`, and config, database and logs are `0600`. |
| Path safety | Drive names containing `/`, `..` etc. are sanitized. Symlinks, FIFOs and sockets are never followed or uploaded. Files outside the sync folder can't be reached. |
| Data safety | Downloads are verified by MD5 and written atomically. If the sync folder disappears (for example, an unmounted disk), sync halts instead of deleting Drive. If more than 50 items vanish at once, deletion waits for your confirmation in the dashboard. |
| `.gdoc` handler | Opens only `https://` URLs on Google Docs/Drive hosts, never arbitrary links. |
| systemd hardening | `NoNewPrivileges`, `RestrictSUIDSGID`, `LockPersonality`, `RestrictRealtime` and a native syscall architecture only. |

## How sync works

```
            inotify (watchdog)                 Drive Changes API (every 30 s)
~/GoogleDrive ─────────┐                     ┌───────────── Google Drive
                       ▼                     ▼
                 local pass ──► SyncEngine ◄── remote pass
                                  │
                       state.db (last agreed state:
                       id, path, md5, mtime, size, inode)
```

- **Initial sync** lists My Drive and builds the folder tree. Files that already exist locally with the same checksum are adopted without downloading.
- **Local pass.** It scans the folder and compares it with `state.db`. A file whose path vanished but whose inode reappears elsewhere is treated as a **rename/move**, so it is moved in Drive, not re-uploaded. Edits are detected by mtime/size and confirmed by MD5. Before uploading an edit, the engine checks that the Drive copy hasn't changed too.
- **Remote pass** applies Drive changes: new files, edits, renames/moves and removals.
- Large files use resumable chunked uploads (8 MiB chunks). Network and API errors are retried with exponential backoff. If you are offline, the dashboard shows "Offline", and sync resumes when the network returns.
- Temporary and lock files are ignored: `*.swp`, `*~`, `.~lock.*#`, `.goutputstream-*`, `*.part` and so on.

## Limitations

- It mirrors a full copy of **My Drive**, so you need enough disk space. There are no on-demand "streamed" files (that would need a FUSE filesystem).
- **Shared drives** and **Shared with me** are not synced. Drive shortcuts are skipped too, because they can point anywhere. To sync a shared folder, move it into My Drive (if you can edit it).
- Google Docs/Sheets/Slides are links, not editable office files.
- No selective sync of sub-folders yet.
- For very large folder trees (more than ~60k folders), raise the inotify limit:
  `echo fs.inotify.max_user_watches=524288 | sudo tee /etc/sysctl.d/90-inotify.conf && sudo sysctl --system`.
  Without it, the client falls back to periodic full scans.

## Troubleshooting

- **"Google rejected the stored login"**: the app is still in *Testing* (7-day expiry), or access was revoked. Publish the app (setup step 2.3), then sign in again.
- **Service didn't start at login**: run `systemctl --user enable --now gdrive-sync` and check `journalctl --user -u gdrive-sync`.
- **Want to rebuild state** (for example, after restoring a backup): Dashboard → Settings → Advanced → *Re-sync from scratch*. This re-matches files by checksum and deletes nothing.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests     # sync engine + web security tests
GDRIVE_SYNC_PORT=18642 .venv/bin/gdrive-sync run -v
```

Code layout: `gdrive_sync/`
- `auth.py`: OAuth and keyring
- `drive.py`: Drive REST client
- `sync.py`: the engine
- `db.py`: state store
- `watcher.py`: inotify
- `web.py`: dashboard
- `security.py`: password hashing and login throttling
