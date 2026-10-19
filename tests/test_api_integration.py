"""API + database integration tests.  STATUS: WRITTEN, NOT YET EXECUTED (the authoring sandbox had no PostgreSQL/FastAPI).

Run against a THROWAWAY database only (its name must contain "test"; tables are emptied between tests):
    createdb rushpos_test
    export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rushpos_test
    pip install -r requirements-dev.txt && python -m pytest tests/test_api_integration.py -v
"""
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
try:
    import psycopg
    from fastapi.testclient import TestClient
    HAVE = True
except Exception:  # pragma: no cover
    HAVE = False

from app.auth import hash_password  # noqa: E402

OWNER_PW, STAFF_PW = "owner-password-1", "staff-password-1"


def _env(mode="production"):
    os.environ.update(DATABASE_URL=TEST_DB, SESSION_SECRET="t" * 48, OWNER_PASSWORD_HASH=hash_password(OWNER_PW),
                      STAFF_PASSWORD_HASH=hash_password(STAFF_PW), APP_MODE=mode, COOKIE_SECURE="true")


def _wipe(keep_marker=None):
    assert "test" in urlparse(TEST_DB).path.lower(), "refusing to wipe a database whose name does not contain 'test'"
    with psycopg.connect(TEST_DB, autocommit=True) as c:
        for t in ("sales", "stock_log", "products", "sample_sets", "settings"):
            c.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        c.execute("DROP SEQUENCE IF EXISTS bill_seq")
        if keep_marker:
            c.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value JSONB NOT NULL)")
            c.execute("INSERT INTO settings VALUES('environment', %s::jsonb)", (f'"{keep_marker}"',))


@unittest.skipUnless(HAVE and TEST_DB, "needs fastapi + TEST_DATABASE_URL (throwaway Postgres)")
class Base(unittest.TestCase):
    mode = "production"

    def setUp(self):
        _wipe(); _env(self.mode)
        from app import main
        main.THROTTLE.__init__()
        self.main = main
        self.client = TestClient(main.app, base_url="https://testserver"); self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def login(self, role="owner", client=None):
        c = client or self.client
        r = c.post("/api/login", json={"role": role, "password": OWNER_PW if role == "owner" else STAFF_PW})
        self.assertEqual(r.status_code, 200, r.text)
        c.headers["X-CSRF-Token"] = r.json()["csrf"]
        return c

    def new_client(self, role):
        c = TestClient(self.main.app, base_url="https://testserver"); return self.login(role, c)

    def product(self, name="Veg Puff", price=25, stock=10):
        r = self.client.post("/api/products", json={"name": name, "cat": "Puffs", "price": price, "stock": stock})
        self.assertEqual(r.status_code, 200, r.text); return r.json()

    def stock(self, pid):
        return next(p["stock"] for p in self.client.get("/api/state").json()["products"] if p["id"] == pid)


class Permissions(Base):
    OWNER_ONLY = [("post", "/api/products", {"name": "x", "cat": "c", "price": 1, "stock": 1}),
                  ("put", "/api/products/1", {"name": "x", "cat": "c", "price": 1}),
                  ("patch", "/api/products/1/availability", {"on": False}),
                  ("post", "/api/products/1/stock", {"add": 1}), ("get", "/api/stock-log", None),
                  ("put", "/api/printer", {"shop_name": "x"})]
    ANY_ROLE = [("get", "/api/state", None), ("post", "/api/sales", {"key": "k" * 10, "items": [{"id": 1, "q": 1}], "pay": "Cash"}),
                ("get", "/api/sales/by-key/abcdefgh", None)]

    def test_unauthenticated_gets_401_everywhere(self):
        for m, p, b in self.OWNER_ONLY + self.ANY_ROLE + [("post", "/api/reset", None), ("post", "/api/restore-products", None)]:
            r = getattr(self.client, m)(p, **({"json": b} if b is not None else {}))
            self.assertIn(r.status_code, (401, 404), f"{m} {p} -> {r.status_code}")  # 404 only for demo-only tools
            self.assertNotEqual(r.status_code, 200)

    def test_staff_cannot_use_owner_endpoints(self):
        self.login("staff")
        for m, p, b in self.OWNER_ONLY:
            r = getattr(self.client, m)(p, **({"json": b} if b is not None else {}))
            self.assertEqual(r.status_code, 403, f"{m} {p} -> {r.status_code}")

    def test_staff_state_has_only_recent_bills(self):
        self.login("owner"); p = self.product(stock=100)
        for i in range(self.main.STAFF_RECENT_BILLS + 5):
            self.assertEqual(self.client.post("/api/sales", json={"key": f"bill-key-{i:04d}", "items": [{"id": p["id"], "q": 1}], "pay": "Cash"}).status_code, 200)
        staff = self.new_client("staff")
        self.assertEqual(len(staff.get("/api/state").json()["sales"]), self.main.STAFF_RECENT_BILLS)
        self.assertGreater(len(self.client.get("/api/state").json()["sales"]), self.main.STAFF_RECENT_BILLS)

    def test_csrf_token_required_for_changes(self):
        self.login("owner"); tok = self.client.headers.pop("X-CSRF-Token")
        self.assertEqual(self.client.post("/api/products", json={"name": "x", "cat": "c", "price": 1, "stock": 1}).status_code, 403)
        self.client.headers["X-CSRF-Token"] = "wrong"
        self.assertEqual(self.client.post("/api/products", json={"name": "x", "cat": "c", "price": 1, "stock": 1}).status_code, 403)
        self.client.headers["X-CSRF-Token"] = tok
        self.assertEqual(self.client.post("/api/products", json={"name": "x", "cat": "c", "price": 1, "stock": 1}).status_code, 200)

    def test_login_throttle_and_cookie_flags(self):
        for _ in range(8):
            self.assertEqual(self.client.post("/api/login", json={"role": "owner", "password": "nope"}).status_code, 401)
        r = self.client.post("/api/login", json={"role": "owner", "password": OWNER_PW})
        self.assertEqual(r.status_code, 429); self.assertIn("retry-after", r.headers)
        self.main.THROTTLE.__init__()
        ck = self.client.post("/api/login", json={"role": "staff", "password": STAFF_PW}).headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=strict"):
            self.assertIn(flag, ck)

    def test_logout_ends_access_and_api_is_not_cacheable(self):
        self.login("owner"); r = self.client.get("/api/state"); self.assertEqual(r.headers["cache-control"], "no-store")
        self.client.post("/api/logout"); self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/state").status_code, 401)


class ProductionIsolation(Base):
    def test_reset_and_restore_do_not_exist_in_production(self):
        self.login("owner"); p = self.product()
        for path in ("/api/reset", "/api/restore-products"):
            self.assertEqual(self.client.post(path).status_code, 404)
        self.assertEqual(self.stock(p["id"]), 10)

    def test_startup_never_seeds_products(self):
        self.login("owner"); self.assertEqual(self.client.get("/api/state").json()["products"], [])

    def test_production_refuses_a_database_marked_demo(self):
        _wipe("demo"); _env("production")
        from app.config import ConfigError
        with self.assertRaises(ConfigError):
            with TestClient(self.main.app):
                pass

    def test_demo_mode_refuses_unmarked_database_with_data(self):
        self.login("owner"); self.product()
        with psycopg.connect(TEST_DB, autocommit=True) as c:  # simulate the pre-existing prototype DB: data, but no marker
            c.execute("DELETE FROM settings WHERE key='environment'")
        _env("demo")
        from app.config import ConfigError
        with self.assertRaises(ConfigError):
            with TestClient(self.main.app):
                pass


class DemoMode(Base):
    mode = "demo"

    def test_demo_seeds_and_can_reset(self):
        self.login("owner"); self.assertGreater(len(self.client.get("/api/state").json()["products"]), 10)
        self.assertEqual(self.client.post("/api/reset").status_code, 200)


class StaleStock(Base):
    def test_availability_toggle_and_edit_cannot_overwrite_stock(self):
        self.login("owner"); p = self.product(stock=10)
        staff = self.new_client("staff")
        self.assertEqual(staff.post("/api/sales", json={"key": "sale-key-0001", "items": [{"id": p["id"], "q": 3}], "pay": "Cash"}).status_code, 200)
        self.assertEqual(self.stock(p["id"]), 7)
        # a stale browser still believes stock is 10 and sends it along; it must be ignored
        self.assertEqual(self.client.patch(f"/api/products/{p['id']}/availability", json={"on": False}).status_code, 200)
        self.assertEqual(self.client.put(f"/api/products/{p['id']}", json={"name": "Veg Puff", "cat": "Puffs", "price": 30, "stock": 10, "on": True}).status_code, 200)
        self.assertEqual(self.stock(p["id"]), 7)
        log = self.client.get("/api/stock-log?limit=50").json()
        self.assertFalse([l for l in log if l["reason"] == "correction"], "an edit must not create a stock correction")

    def test_set_to_is_rejected_when_stock_changed_since_viewed(self):
        self.login("owner"); p = self.product(stock=10)
        self.client.post("/api/sales", json={"key": "sale-key-0002", "items": [{"id": p["id"], "q": 2}], "pay": "Cash"})
        r = self.client.post(f"/api/products/{p['id']}/stock", json={"set_to": 12, "expected": 10})
        self.assertEqual(r.status_code, 409); self.assertEqual(self.stock(p["id"]), 8)
        self.assertEqual(self.client.post(f"/api/products/{p['id']}/stock", json={"set_to": 12, "expected": 8}).status_code, 200)

    def test_no_negative_stock_via_adjustment(self):
        self.login("owner"); p = self.product(stock=2)
        self.assertEqual(self.client.post(f"/api/products/{p['id']}/stock", json={"add": -3}).status_code, 409)


class Checkout(Base):
    def bill(self, client, pid, q, key, pay="Cash"):
        return client.post("/api/sales", json={"key": key, "items": [{"id": pid, "q": q}], "pay": pay})

    def test_retry_with_same_key_returns_same_bill_and_deducts_once(self):
        self.login("owner"); p = self.product(stock=10)
        a, b = self.bill(self.client, p["id"], 3, "retry-key-001"), self.bill(self.client, p["id"], 3, "retry-key-001")
        self.assertEqual((a.status_code, b.status_code), (200, 200)); self.assertEqual(a.json()["no"], b.json()["no"])
        self.assertEqual(self.stock(p["id"]), 7); self.assertEqual(len(self.client.get("/api/state").json()["sales"]), 1)

    def test_same_key_different_bill_is_refused(self):
        self.login("owner"); p = self.product(stock=10)
        self.assertEqual(self.bill(self.client, p["id"], 1, "reuse-key-001").status_code, 200)
        self.assertEqual(self.bill(self.client, p["id"], 2, "reuse-key-001").status_code, 409); self.assertEqual(self.stock(p["id"]), 9)

    def test_concurrent_identical_requests_make_one_sale(self):
        self.login("owner"); p = self.product(stock=3)
        clients = [self.new_client("staff") for _ in range(6)]
        with ThreadPoolExecutor(6) as ex:
            rs = list(ex.map(lambda c: self.bill(c, p["id"], 3, "race-same-key"), clients))
        self.assertEqual([r.status_code for r in rs], [200] * 6, [r.text for r in rs])  # retries must NOT see "only 0 in stock"
        self.assertEqual(len({r.json()["no"] for r in rs}), 1); self.assertEqual(self.stock(p["id"]), 0)

    def test_two_devices_cannot_sell_the_last_item(self):
        self.login("owner"); p = self.product(stock=1)
        c1, c2 = self.new_client("staff"), self.new_client("staff"); go = threading.Barrier(2)

        def sell(c, k):
            go.wait(); return self.bill(c, p["id"], 1, k)
        with ThreadPoolExecutor(2) as ex:
            rs = list(ex.map(lambda a: sell(*a), [(c1, "last-item-key-1"), (c2, "last-item-key-2")]))
        self.assertEqual(sorted(r.status_code for r in rs), [200, 409]); self.assertEqual(self.stock(p["id"]), 0)

    def test_failed_checkout_does_not_deduct_and_client_price_is_ignored(self):
        self.login("owner"); a, b = self.product("A", 10, 5), self.product("B", 20, 1)
        r = self.client.post("/api/sales", json={"key": "fail-key-0001", "pay": "Cash", "items": [{"id": a["id"], "q": 2}, {"id": b["id"], "q": 2}]})
        self.assertEqual(r.status_code, 409); self.assertEqual((self.stock(a["id"]), self.stock(b["id"])), (5, 1))
        ok = self.client.post("/api/sales", json={"key": "ok-key-000001", "pay": "Cash", "items": [{"id": a["id"], "q": 2, "price": 1}]})
        self.assertEqual(ok.json()["total"], 20)  # server price, not the client-supplied 1

    def test_missing_idempotency_key_is_rejected(self):
        self.login("owner"); p = self.product()
        self.assertEqual(self.client.post("/api/sales", json={"items": [{"id": p["id"], "q": 1}], "pay": "Cash"}).status_code, 422)

    def test_by_key_lookup(self):
        self.login("owner"); p = self.product()
        self.assertEqual(self.client.get("/api/sales/by-key/never-used-key").status_code, 404)
        self.bill(self.client, p["id"], 1, "lookup-key-001"); self.assertEqual(self.client.get("/api/sales/by-key/lookup-key-001").status_code, 200)


if __name__ == "__main__":
    unittest.main()
