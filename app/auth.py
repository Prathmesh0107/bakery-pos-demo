"""Authentication primitives (stdlib only, no database tables needed).

Phase 0 model: two role accounts (owner, staff), password hashes supplied by environment variables,
stateless HMAC-signed session cookie. See docs/PHASE0.md for the trade-offs.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Dict, List, Optional

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
COOKIE = "rpos_session"
ROLES = ("owner", "staff")


# ---------- password hashing ----------
def hash_password(pw: str, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: Optional[str]) -> bool:
    """Constant-time. Always burns one scrypt so a missing account is not distinguishable by timing."""
    try:
        _, n, r, p, salt, want = (stored or "").split("$")
        salt_b, want_b = bytes.fromhex(salt), bytes.fromhex(want)
        n, r, p = int(n), int(r), int(p)
        if n > 2 ** 20 or r > 32 or p > 4:  # refuse absurd cost parameters from a malformed value
            raise ValueError
    except Exception:
        hashlib.scrypt(pw.encode(), salt=b"x" * 16, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
        return False
    got = hashlib.scrypt(pw.encode(), salt=salt_b, n=n, r=r, p=p, dklen=len(want_b), maxmem=256 * 1024 * 1024)
    return hmac.compare_digest(got, want_b)


# ---------- signed session cookie ----------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sig(secret: str, msg: str) -> str:
    return _b64(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest())


def make_session(secret: str, role: str, hours: int, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    body = _b64(json.dumps({"r": role, "e": int(now + hours * 3600), "i": secrets.token_hex(8)},
                           separators=(",", ":")).encode())
    return body + "." + _sig(secret, body)


def read_session(secret: str, token: Optional[str], now: Optional[float] = None) -> Optional[dict]:
    """Returns {"role","sid","csrf"} or None for anything missing, tampered, malformed or expired."""
    if not token or token.count(".") != 1:
        return None
    body, sig = token.split(".")
    if not hmac.compare_digest(sig, _sig(secret, body)):
        return None
    try:
        d = json.loads(_unb64(body))
        role, exp, sid = d["r"], int(d["e"]), str(d["i"])
    except Exception:
        return None
    if role not in ROLES or (time.time() if now is None else now) >= exp:
        return None
    return {"role": role, "sid": sid, "csrf": csrf_for(secret, sid)}


def csrf_for(secret: str, sid: str) -> str:
    return _sig(secret, "csrf:" + sid)


# ---------- login throttle (in memory, per role) ----------
class Throttle:
    """After MAX failures inside WINDOW seconds a role is locked for LOCK seconds.

    In-memory: resets on restart and is per process (fine for one instance; see docs/PHASE0.md).
    Keyed by role, not IP, because behind Render's proxy every client shares one address. Trade-off:
    someone guessing wrong passwords can lock the login for LOCK seconds (a nuisance, not a breach).
    """

    def __init__(self, max_fail=8, window=600, lock=300):
        self.max_fail, self.window, self.lock = max_fail, window, lock
        self.fails: Dict[str, List[float]] = {}
        self.locked_until: Dict[str, float] = {}

    def retry_after(self, key: str, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        left = self.locked_until.get(key, 0) - now
        return int(left) + 1 if left > 0 else 0

    def failure(self, key: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        f = [t for t in self.fails.get(key, []) if now - t < self.window] + [now]
        self.fails[key] = f
        if len(f) >= self.max_fail:
            self.locked_until[key] = now + self.lock
            self.fails[key] = []

    def success(self, key: str) -> None:
        self.fails.pop(key, None)
