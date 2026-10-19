import unittest

from app.auth import hash_password
from app.config import ConfigError, load_settings

H1, H2 = hash_password("owner-pass-123"), hash_password("staff-pass-123")
GOOD = {"DATABASE_URL": "postgresql://u:p@db.example/rush", "SESSION_SECRET": "s" * 40, "OWNER_PASSWORD_HASH": H1}


def env(**kw):
    e = dict(GOOD); e.update(kw)
    return {k: v for k, v in e.items() if v is not None}


class ConfigFailsClosed(unittest.TestCase):
    def test_valid_production_defaults(self):
        s = load_settings(env())
        self.assertEqual(s.mode, "production"); self.assertTrue(s.cookie_secure); self.assertIsNone(s.staff_hash)

    def test_missing_database_url_has_no_localhost_fallback(self):
        with self.assertRaises(ConfigError) as cm:
            load_settings(env(DATABASE_URL=None))
        self.assertIn("DATABASE_URL", str(cm.exception)); self.assertNotIn("postgres:postgres@", str(cm.exception))

    def test_empty_environment_lists_every_problem(self):
        with self.assertRaises(ConfigError) as cm:
            load_settings({})
        for k in ("DATABASE_URL", "SESSION_SECRET", "OWNER_PASSWORD_HASH"):
            self.assertIn(k, str(cm.exception))

    def test_short_or_missing_session_secret_rejected(self):
        for bad in (None, "", "short", "x" * 31):
            with self.assertRaises(ConfigError):
                load_settings(env(SESSION_SECRET=bad))

    def test_owner_hash_must_be_valid_and_plaintext_is_rejected(self):
        for bad in (None, "", "1234", "plaintext-password", "scrypt$a$b$c$d$e"):
            with self.assertRaises(ConfigError):
                load_settings(env(OWNER_PASSWORD_HASH=bad))

    def test_staff_hash_optional_but_validated_and_distinct(self):
        self.assertEqual(load_settings(env(STAFF_PASSWORD_HASH=H2)).staff_hash, H2)
        with self.assertRaises(ConfigError):
            load_settings(env(STAFF_PASSWORD_HASH="nope"))
        with self.assertRaises(ConfigError):
            load_settings(env(STAFF_PASSWORD_HASH=H1))

    def test_unknown_mode_rejected_and_unset_means_production(self):
        with self.assertRaises(ConfigError):
            load_settings(env(APP_MODE="development"))
        self.assertEqual(load_settings(env(APP_MODE="")).mode, "production")

    def test_insecure_cookie_only_allowed_in_demo(self):
        with self.assertRaises(ConfigError):
            load_settings(env(COOKIE_SECURE="false"))
        self.assertFalse(load_settings(env(COOKIE_SECURE="false", APP_MODE="demo")).cookie_secure)

    def test_non_postgres_url_rejected(self):
        with self.assertRaises(ConfigError):
            load_settings(env(DATABASE_URL="sqlite:///x.db"))


if __name__ == "__main__":
    unittest.main()
