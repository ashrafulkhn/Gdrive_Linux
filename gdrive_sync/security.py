"""Local UI password hashing and login throttling."""

import base64
import hashlib
import hmac
import os
import threading
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
MIN_PASSWORD_LENGTH = 10


def _scrypt(password, salt, n, r, p):
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)


def hash_password(password):
    salt = os.urandom(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password, stored):
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        candidate = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(candidate, base64.b64decode(digest))
    except (ValueError, AttributeError):
        return False


class LoginLimiter:
    """Exponential lockout after repeated failed logins."""

    def __init__(self, free_attempts=5):
        self.free_attempts = free_attempts
        self.failures = 0
        self.locked_until = 0.0
        self._lock = threading.Lock()

    def wait_seconds(self):
        with self._lock:
            return max(0, int(self.locked_until - time.time() + 0.999))

    def failed(self):
        with self._lock:
            self.failures += 1
            if self.failures >= self.free_attempts:
                self.locked_until = time.time() + min(3600, 30 * 2 ** (self.failures - self.free_attempts))

    def succeeded(self):
        with self._lock:
            self.failures = 0
            self.locked_until = 0.0
