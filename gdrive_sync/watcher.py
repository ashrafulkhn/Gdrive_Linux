"""inotify-based change notification (via watchdog) for the sync folder."""

import fnmatch
import logging
import os

log = logging.getLogger(__name__)

IGNORED_EVENTS = {"opened", "closed_no_write"}


class LocalWatcher:
    def __init__(self, engine, ignore_patterns):
        self.engine = engine
        self.ignore_patterns = ignore_patterns
        self.observer = None
        self.path = None

    def _ignored(self, path):
        name = os.path.basename(path)
        return any(fnmatch.fnmatchcase(name, p) for p in self.ignore_patterns)

    def ensure(self, path):
        """Watch ``path`` (restarting if it changed). Falls back to periodic scans on failure."""
        path = str(path)
        if self.path == path and self.observer and self.observer.is_alive():
            return
        self.stop()
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.warning("watchdog not installed; relying on periodic scans")
            self.path = path
            return

        watcher = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.event_type in IGNORED_EVENTS:
                    return
                paths = [p for p in (event.src_path, getattr(event, "dest_path", "")) if p]
                if paths and all(watcher._ignored(os.fsdecode(p)) for p in paths):
                    return
                watcher.engine.notify_local_change()

        try:
            self.observer = Observer()
            self.observer.schedule(Handler(), path, recursive=True)
            self.observer.start()
            log.info("Watching %s for changes", path)
        except OSError as e:
            log.warning(
                "Cannot watch %s (%s). Falling back to periodic scans; for large folders raise "
                "fs.inotify.max_user_watches (see README).", path, e
            )
            self.observer = None
        self.path = path

    def stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None
