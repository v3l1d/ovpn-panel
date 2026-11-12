import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        expected = base64.b64decode(dk)
        got = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
        return hmac.compare_digest(got, expected)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LoginThrottle:
    """Blocca un IP dopo `limit` tentativi falliti in `window` secondi."""

    def __init__(self, limit=5, window=600):
        self.limit, self.window = limit, window
        self.failures = defaultdict(deque)

    def _prune(self, key):
        q = self.failures[key]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, key) -> bool:
        return len(self._prune(key)) >= self.limit

    def fail(self, key):
        self._prune(key).append(time.monotonic())

    def reset(self, key):
        self.failures.pop(key, None)
