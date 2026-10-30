// Dashboard: polls /api/status and wires the action buttons.
// All dynamic text is set via textContent - file names from Drive are untrusted.
(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const $ = (id) => document.getElementById(id);

  const disconnect = $("disconnect-form");
  if (disconnect) {
    disconnect.addEventListener("submit", (ev) => {
      if (!confirm("Disconnect and revoke access to your Google account?")) ev.preventDefault();
    });
  }
  if (!$("status-card")) return;

  const TITLES = {
    idle: "Up to date",
    syncing: "Syncing…",
    paused: "Paused",
    offline: "Offline",
    disconnected: "Not connected",
    attention: "Needs your attention",
    error: "Sync problem",
    auth_error: "Sign-in needed",
    starting: "Starting…",
  };
  const KINDS = { up: "Uploaded", down: "Downloaded", move: "Moved", delete: "Removed", conflict: "Conflict", error: "Error", info: "Info" };

  async function post(url, question) {
    if (question && !confirm(question)) return;
    await fetch(url, { method: "POST", headers: { "X-CSRF-Token": csrf }, credentials: "same-origin" });
    refresh();
  }

  function fmtBytes(n) {
    if (n == null) return "";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + u[i];
  }

  function ago(ts) {
    if (!ts) return "-";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    return new Date(ts * 1000).toLocaleString();
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function renderList(ul, items, emptyText, row) {
    ul.replaceChildren();
    if (!items.length) { ul.append(el("li", "muted", emptyText)); return; }
    for (const it of items) ul.append(row(it));
  }

  function render(s) {
    if (!s.connected) { location.reload(); return; }
    const state = s.paused ? "paused" : s.state;
    $("status-icon").className = "status-icon " + state;
    $("status-title").textContent = TITLES[state] || state;
    $("status-detail").textContent = s.paused ? "Changes are not being synced." : (s.detail || "");
    $("btn-pause").textContent = s.paused ? "Resume" : "Pause";
    $("btn-pause").dataset.paused = s.paused ? "1" : "";
    $("fact-folder").textContent = s.sync_dir;
    $("fact-count").textContent = s.files_tracked.toLocaleString();
    $("fact-last").textContent = ago(s.last_sync);
    $("fact-pending").textContent = s.pending_changes ? s.pending_changes.toLocaleString() + " change(s)" : "Nothing";
    $("fact-cache").textContent = `${fmtBytes(s.cached_bytes)} downloaded` + (s.keep_all_offline ? " (keeping everything)" : ` · limit ${fmtBytes(s.cache_limit)}`);
    $("cache-bar").style.width = s.cache_limit ? Math.min(100, (100 * s.cached_bytes) / s.cache_limit).toFixed(1) + "%" : "0";
    if (s.account) {
      const email = $("account-email");
      if (email) email.textContent = s.account.email || "";
      const used = s.account.usage, lim = s.account.limit;
      $("fact-quota").textContent = lim ? `${fmtBytes(used)} of ${fmtBytes(lim)}` : `${fmtBytes(used)} used`;
      $("quota-bar").style.width = lim ? Math.min(100, (100 * used) / lim).toFixed(1) + "%" : "0";
    }

    const md = s.pending_mass_delete;
    $("mass-delete").classList.toggle("hidden", !md);
    if (md) {
      $("md-count").textContent = md.count.toLocaleString();
      renderList($("md-examples"), md.examples, "", (p) => el("li", null, p));
    }

    renderList($("activity"), s.activity, "Nothing yet.", (a) => {
      const li = el("li");
      li.append(el("span", "kind " + a.kind, KINDS[a.kind] || a.kind));
      const p = el("span", "path", a.path);
      if (a.detail) p.append(el("span", "sub", a.detail));
      li.append(p, el("span", "when", ago(a.time)));
      return li;
    });
    renderList($("conflicts"), s.conflicts, "No conflicts.", (c) => {
      const li = el("li");
      li.append(el("span", "kind conflict", "Conflict"));
      const p = el("span", "path", c.path);
      p.append(el("span", "sub", "Your version: " + c.copy));
      li.append(p, el("span", "when", ago(c.time)));
      return li;
    });
  }

  async function refresh() {
    try {
      const r = await fetch("/api/status", { credentials: "same-origin", cache: "no-store" });
      if (r.status === 401) { location.href = "/login"; return; }
      render(await r.json());
    } catch (e) {
      $("status-title").textContent = "Service not reachable";
      $("status-detail").textContent = "Is the gdrive-sync service running?";
      $("status-icon").className = "status-icon error";
    }
  }

  $("btn-sync").addEventListener("click", () => post("/api/sync-now"));
  $("btn-pause").addEventListener("click", (e) => post(e.target.dataset.paused ? "/api/resume" : "/api/pause"));
  $("btn-confirm-delete").addEventListener("click", () => post("/api/confirm-deletes", "Move these items to the Google Drive trash?"));
  $("btn-restore").addEventListener("click", () => post("/api/restore-deletes"));
  const resync = $("btn-resync");
  if (resync) resync.addEventListener("click", () => post("/api/resync", "Re-read everything from Drive and empty the local cache?"));

  refresh();
  setInterval(refresh, 2500);
})();
