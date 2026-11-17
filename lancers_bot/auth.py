"""The owner's password: the only way into the server (the extension and the Grok Drafter send it).

The bot has one user, its owner. `python manage.py set-password` stores a scrypt hash of the password
in the database; the plain password is never stored. The extension sends the password where it used to
send its token (Settings → Token), so the extension itself needs no change.

Guessing is slowed three ways: scrypt makes each check expensive, a wrong password waits
FAIL_DELAY_SECONDS before the answer, and an address with FAIL_LIMIT wrong passwords within
FAIL_WINDOW_SECONDS is refused for LOCKOUT_SECONDS (even with the right password).
"""

import hashlib
import hmac
import secrets
import time

MIN_PASSWORD_CHARS = 12
# scrypt cost: about 0.1 s and 32 MB per check
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024

FAIL_LIMIT = 5
FAIL_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60
FAIL_DELAY_SECONDS = 1.0


def check_strength(password):
    """Raise ValueError when the password is too weak to protect a server on the internet"""
    if len(password or "") < MIN_PASSWORD_CHARS:
        raise ValueError(f"the password must be at least {MIN_PASSWORD_CHARS} characters")
    if len(set(password)) < 6:
        raise ValueError("the password repeats too few different characters")


def hash_password(password):
    check_strength(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    """True when password matches the stored hash (constant-time compare); False for anything malformed"""
    if not password or not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                                   maxmem=SCRYPT_MAXMEM)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, bytes.fromhex(digest))


class Lockout:
    """Wrong-password bookkeeping per client address (in memory: a server restart forgets it)"""

    def __init__(self, limit=FAIL_LIMIT, window=FAIL_WINDOW_SECONDS, lockout=LOCKOUT_SECONDS):
        self.limit, self.window, self.lockout = limit, window, lockout
        self.failures = {}       # address -> [timestamps of wrong passwords]
        self.locked = {}         # address -> locked until (timestamp)

    def is_locked(self, address, now=None):
        now = time.time() if now is None else now
        until = self.locked.get(address)
        if until and until > now:
            return True
        self.locked.pop(address, None)
        return False

    def failed(self, address, now=None):
        """Record a wrong password; returns True when this one locks the address out"""
        now = time.time() if now is None else now
        recent = [t for t in self.failures.get(address, []) if now - t < self.window] + [now]
        self.failures[address] = recent
        # Forget addresses that have gone quiet, so the table can't grow without bound
        for other in [a for a, ts in self.failures.items() if now - ts[-1] >= self.window]:
            del self.failures[other]
        if len(recent) >= self.limit:
            self.locked[address] = now + self.lockout
            self.failures.pop(address, None)
            return True
        return False

    def succeeded(self, address):
        self.failures.pop(address, None)
