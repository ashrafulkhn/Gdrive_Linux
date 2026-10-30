"""Service lifecycle helpers."""

import tempfile
import threading
import unittest

from gdrive_sync.__main__ import watch_mount


class WatchMountTestCase(unittest.TestCase):
    def test_notices_when_folder_is_no_longer_mounted(self):
        # an ordinary directory stands in for a mount that was ejected (lazily detached)
        mounted, ejected = threading.Event(), threading.Event()
        mounted.set()
        t = threading.Thread(target=watch_mount, args=(tempfile.mkdtemp(), mounted, ejected.set, 0.01), daemon=True)
        t.start()
        self.assertTrue(ejected.wait(2))

    def test_waits_until_the_mount_is_up(self):
        mounted, ejected = threading.Event(), threading.Event()
        t = threading.Thread(target=watch_mount, args=(tempfile.mkdtemp(), mounted, ejected.set, 0.01), daemon=True)
        t.start()
        self.assertFalse(ejected.wait(0.2))


if __name__ == "__main__":
    unittest.main()
