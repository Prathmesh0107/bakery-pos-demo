"""Startup configuration. Fails CLOSED: missing or insecure settings stop the app from starting.

No default database URL, no default secrets, no "open" mode. Pure stdlib so it can be unit-tested anywhere.
"""
from dataclasses import dataclass
from typing import Mapping, Optional

MODES = ("production", "demo")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    mode: str                 # "production" (default) or "demo"
    database_url: str
    session_secret: str
    owner_hash: str
    staff_hash: Optional[str]  # None = counter-staff login disabled
    cookie_secure: bool
    session_hours: int

    @property
    def is_demo(self) -> bool:
        return self.mode == "demo"


def _looks_like_hash(v: str) -> bool:
    p = v.split("$")
    return len(p) == 6 and p[0] == "scrypt" and all(p[1:4]) and p[1].isdigit() and p[2].isdigit() and p[3].isdigit()


def load_settings(env: Mapping[str, str]) -> Settings:
    g = lambda k: (env.get(k) or "").strip()
    errs = []

    # An unset APP_MODE is production on purpose: the safe mode is the default.
    mode = g("APP_MODE").lower() or "production"
    if mode not in MODES:
        errs.append(f"APP_MODE must be one of {MODES} (got {mode!r})")

    db = g("DATABASE_URL")
    if not db:
        errs.append("DATABASE_URL is required (there is no localhost fallback)")
    elif not db.startswith(("postgresql://", "postgres://")):
        errs.append("DATABASE_URL must start with postgresql://")

    secret = g("SESSION_SECRET")
    if len(secret) < 32:
        errs.append("SESSION_SECRET is required and must be at least 32 characters "
                    "(generate with: python -c \"import secrets;print(secrets.token_urlsafe(48))\")")

    owner = g("OWNER_PASSWORD_HASH")
    if not _looks_like_hash(owner):
        errs.append("OWNER_PASSWORD_HASH is required (generate with: python scripts/hash_password.py)")

    staff = g("STAFF_PASSWORD_HASH") or None
    if staff and not _looks_like_hash(staff):
        errs.append("STAFF_PASSWORD_HASH is set but is not a valid hash (python scripts/hash_password.py)")
    if staff and staff == owner:
        errs.append("STAFF_PASSWORD_HASH must differ from OWNER_PASSWORD_HASH")

    cs = g("COOKIE_SECURE").lower() or "true"
    if cs not in ("true", "false"):
        errs.append("COOKIE_SECURE must be true or false")
    cookie_secure = cs != "false"
    if not cookie_secure and mode != "demo":
        errs.append("COOKIE_SECURE=false is only allowed with APP_MODE=demo (local http testing)")

    try:
        hours = int(g("SESSION_HOURS") or "12")
        if not 1 <= hours <= 24:
            raise ValueError
    except ValueError:
        errs.append("SESSION_HOURS must be a whole number from 1 to 24")
        hours = 12

    if errs:
        raise ConfigError("Refusing to start - fix these settings:\n  - " + "\n  - ".join(errs))
    return Settings(mode, db, secret, owner, staff, cookie_secure, hours)
