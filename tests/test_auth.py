import unittest

from app import auth

SECRET = "k" * 48


class Passwords(unittest.TestCase):
    def test_roundtrip_and_wrong_password(self):
        h = auth.hash_password("correct horse")
        self.assertTrue(auth.verify_password("correct horse", h)); self.assertFalse(auth.verify_password("wrong", h))

    def test_salted_hashes_differ_and_plaintext_not_stored(self):
        a, b = auth.hash_password("same-pass-1"), auth.hash_password("same-pass-1")
        self.assertNotEqual(a, b); self.assertNotIn("same-pass-1", a)

    def test_missing_or_malformed_hash_is_false_not_error(self):
        for bad in (None, "", "garbage", "scrypt$1$2$3$zz$yy", "scrypt$99999999$8$1$00$00"):
            self.assertFalse(auth.verify_password("x", bad))


class Sessions(unittest.TestCase):
    def test_valid_session(self):
        s = auth.read_session(SECRET, auth.make_session(SECRET, "owner", 12))
        self.assertEqual(s["role"], "owner"); self.assertTrue(s["csrf"])

    def test_expired(self):
        t = auth.make_session(SECRET, "staff", 1, now=1000)
        self.assertIsNotNone(auth.read_session(SECRET, t, now=1000 + 3599)); self.assertIsNone(auth.read_session(SECRET, t, now=1000 + 3600))

    def test_wrong_secret_and_tampering_rejected(self):
        t = auth.make_session(SECRET, "staff", 12)
        self.assertIsNone(auth.read_session("z" * 48, t))
        body, sig = t.split(".")
        forged = auth._b64(auth._unb64(body).replace(b'"staff"', b'"owner"')) + "." + sig  # staff -> owner escalation
        self.assertIsNone(auth.read_session(SECRET, forged))
        for junk in (None, "", "abc", "a.b.c", "."):
            self.assertIsNone(auth.read_session(SECRET, junk))

    def test_csrf_is_per_session_and_secret_bound(self):
        a = auth.read_session(SECRET, auth.make_session(SECRET, "owner", 12))
        b = auth.read_session(SECRET, auth.make_session(SECRET, "owner", 12))
        self.assertNotEqual(a["csrf"], b["csrf"]); self.assertNotEqual(a["csrf"], auth.csrf_for("z" * 48, a["sid"]))


class Throttle(unittest.TestCase):
    def test_locks_after_max_failures_then_expires(self):
        t = auth.Throttle(max_fail=3, window=600, lock=300)
        for i in range(3): t.failure("owner", now=100 + i)
        self.assertGreater(t.retry_after("owner", now=105), 0); self.assertEqual(t.retry_after("owner", now=100 + 2 + 301), 0)
        self.assertEqual(t.retry_after("staff", now=105), 0)  # other role unaffected

    def test_old_failures_age_out_and_success_resets(self):
        t = auth.Throttle(max_fail=3, window=60, lock=300)
        t.failure("o", now=0); t.failure("o", now=1); t.failure("o", now=1000)
        self.assertEqual(t.retry_after("o", now=1001), 0)
        t.failure("o", now=1002); t.success("o"); t.failure("o", now=1003)
        self.assertEqual(t.retry_after("o", now=1004), 0)


if __name__ == "__main__":
    unittest.main()
