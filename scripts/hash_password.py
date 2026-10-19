#!/usr/bin/env python3
"""Generate a password hash for OWNER_PASSWORD_HASH / STAFF_PASSWORD_HASH. Run locally; never commit the password."""
import getpass
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.auth import hash_password  # noqa: E402

pw = getpass.getpass("Password (min 10 characters): ")
if len(pw) < 10:
    sys.exit("Too short - use at least 10 characters.")
if pw != getpass.getpass("Repeat password: "):
    sys.exit("Passwords did not match.")
print(hash_password(pw))
