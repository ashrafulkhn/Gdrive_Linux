# GDrive Sync for Linux

Google Drive on Ubuntu, like Google Drive for desktop on Windows. **My Drive**
and all your **Shared drives** appear in `~/GoogleDrive` right away, but a file
downloads only when you open it. Changes sync both ways, and status icons in
Files show where each file is.

```
~/GoogleDrive/
├── My Drive/
└── Shared drives/
    ├── Accounts/
    └── Sales/
```



| Icon | Meaning |
|---|---|
| <img src="gdrive_sync/static/emblems/emblem-gdrive-cloud.svg" width="16"> | Online only (no disk space used; downloads when opened) |
| <img src="gdrive_sync/static/emblems/emblem-gdrive-local.svg" width="16"> | On this computer and in Drive |
| <img src="gdrive_sync/static/emblems/emblem-gdrive-pinned.svg" width="16"> | Always available offline |
| <img src="gdrive_sync/static/emblems/emblem-gdrive-syncing.svg" width="16"> | Syncing |
| <img src="gdrive_sync/static/emblems/emblem-gdrive-error.svg" width="16"> | Problem (details in the dashboard) |

## Screenshot
<img width="1919" height="664" alt="Screenshot from 2026-10-31 16-30-16" src="https://github.com/user-attachments/assets/0430910c-c0e5-4d81-b535-a8f79e48bf66" />


## 1. Install

Tested on Ubuntu 24.04 (GNOME).

```bash
sudo apt install python3-venv fuse3 python3-nautilus
git clone https://github.com/ashrafulkhn/Gdrive_Linux.git
cd Gdrive_Linux
./install.sh
```

The installer sets up an autostart service, the Files integration and an app
menu entry, **GDrive Sync**. It then prints a setup link. If the browser
doesn't open, paste that link into Firefox.

On the setup page, create a **dashboard password**. It protects the local
dashboard at http://127.0.0.1:8642 and is not your Google password.

## 2. Google Cloud Console (one time, about 5 min)

Sign in with the Google account whose Drive you want to sync.

1. **Create a project:** https://console.cloud.google.com/projectcreate
   - Name it `GDrive Sync`, then click **Create**. Keep it selected in the top bar.
2. **Enable the Drive API:** https://console.cloud.google.com/apis/library/drive.googleapis.com
   - Click **Enable**.
3. **Configure sign-in:** https://console.cloud.google.com/auth/overview
   - Click **Get started**.
   - Enter the app name and your email. Choose **External** as the audience.
   - Enter a contact email, agree, then click **Create**.
4. **Publish the app:** https://console.cloud.google.com/auth/audience
   - Click **Publish app**, then **Confirm**.
   - If you skip this, Google signs you out every 7 days.
5. **Create the client:** https://console.cloud.google.com/auth/clients
   - Click **Create client**.
   - Set the type to **Desktop app**, then click **Create**.
   - Click **Download JSON** right away. Google shows the secret only once.

## 3. Connect

In the dashboard (http://127.0.0.1:8642):

1. **Step 1:** choose the downloaded `client_secret_….json`, then click **Save client**.
2. **Step 2:** click **Sign in with Google** and pick your account.
   - Google warns that the app isn't verified, because you created it yourself. Click **Advanced → Go to GDrive Sync**.
   - **Tick the Google Drive permission**, then click **Continue**.

Your Drive appears in `~/GoogleDrive` (Files sidebar: **Google Drive**)
within seconds, and new shared drives appear within 30 seconds of you joining
them. Delete the JSON from `~/Downloads` afterwards.

## 4. Daily use

| Task | How |
|---|---|
| Keep a file or folder offline | Right-click → **Make available offline** |
| Free disk space | Right-click → **Free up space** (stays in Drive) |
| Download everything (mirror) | Dashboard → Settings → *Keep all files on this computer* (includes shared drives) |
| Delete | Delete key in Files, or `rm`. Items go to the Drive trash, or the shared drive's trash. |
| Open the dashboard | App menu → **GDrive Sync** |

### Shared drives

Your role in each shared drive decides what you can do:

| Role | Open | Edit | Add | Move | Delete |
|---|---|---|---|---|---|
| Manager, Content manager | ✓ | ✓ | ✓ | ✓ | ✓ |
| Contributor | ✓ | ✓ | ✓ | – | – |
| Commenter, Viewer | ✓ | – | – | – | – |

The app follows what Google reports for each file. Anything your role doesn't
allow shows as read-only or "Permission denied".
If Google refuses a change, it is undone here. Your edits are kept as a copy.

Moving a file between drives works as a copy followed by a delete, so the
item gets a new Drive ID and its sharing links change.

## 5. Stop, unmount, log out

| Action | What happens |
|---|---|
| **Log out / shut down** | The folder is unmounted automatically. Edits not yet uploaded are kept and sync at the next login. |
| **Log in** | The folder mounts automatically. |
| **Eject** "Google Drive" in Files | Sync stops until the next login. To mount it again now, open **GDrive Sync** from the app menu. |
| Stop from a terminal | `systemctl --user stop gdrive-sync` |
| Start from a terminal | `systemctl --user start gdrive-sync` |
| Pause syncing, keep the folder | Dashboard → **Pause** |
| Sign out of Google | Dashboard → Settings → Advanced → **Disconnect** (revokes access; files in Drive are untouched) |

## 6. Uninstall

```bash
./uninstall.sh           # keep settings and cache
./uninstall.sh --purge   # also remove settings, cache and the Google login
```

## Troubleshooting

| Problem | Fix |
|---|---|
| Folder is empty | Google isn't connected yet. See step 3. |
| No icons in Files | `sudo apt install python3-nautilus && nautilus -q` |
| "Google rejected the stored login" | Publish the app (step 2.4), then sign in again. |
| Forgot dashboard password | `gdrive-sync reset-password && systemctl --user restart gdrive-sync && gdrive-sync open` |
| Logs | `journalctl --user -u gdrive-sync -f` |

## Security

- The dashboard listens on `127.0.0.1` only. It is password-protected, with CSRF protection and a strict CSP.
- Google sign-in uses OAuth with PKCE, and the login token is stored in GNOME Keyring. Only the Drive scope is requested.
- The Drive folder, cache and settings are readable only by your user.
- Deleted files go to the Drive trash. Mass deletions need confirmation, and both-side edits keep a conflict copy.

Tests: `python3 -m unittest discover -s tests`
