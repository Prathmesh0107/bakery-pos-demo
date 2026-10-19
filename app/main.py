"""Rush Counter POS (independent concept demo) - FastAPI + PostgreSQL.

Not connected to, authorised by, or integrated with any franchise billing software.
"""
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import auth
from .rules import requested_qty, same_request
from .config import ConfigError, Settings, load_settings

log = logging.getLogger("rushpos")
THROTTLE = auth.Throttle()
STAFF_RECENT_BILLS = 30  # counter staff only ever receive this many recent bills (for reprint / repeat-last-bill)
CFG: Optional[Settings] = None  # set in lifespan from validated environment; there is no default database URL
STATIC = Path(__file__).resolve().parent.parent / "static"
CATS = ["Cakes", "Pastries", "Puffs & Savouries", "Bread & Bakery", "Cookies & Chocolates", "Candles & Birthday Accessories"]

# Sample data only (used when the products table is empty) - not official prices.
SEED = [
    ("Chocolate Truffle Cake 500g", CATS[0], 450, 8), ("Black Forest Cake 500g", CATS[0], 400, 6),
    ("Pineapple Cake 500g", CATS[0], 380, 5), ("Black Forest Pastry", CATS[1], 60, 24),
    ("Pineapple Pastry", CATS[1], 55, 20), ("Chocolate Pastry", CATS[1], 65, 18),
    ("Veg Puff", CATS[2], 25, 30), ("Paneer Puff", CATS[2], 35, 20), ("Veg Patty", CATS[2], 30, 3),
    ("Sandwich Bread", CATS[3], 45, 15), ("Pav (6 pcs)", CATS[3], 30, 12), ("Cupcake", CATS[3], 30, 25),
    ("Butter Cookies 200g", CATS[4], 90, 14), ("Choco Bar", CATS[4], 40, 0),
    ("Birthday Candles", CATS[5], 20, 40), ("Number Candle", CATS[5], 30, 15), ("Party Cap (pack)", CATS[5], 50, 10),
    # more sample products (added in the second sample set)
    ("Butterscotch Cake 500g", CATS[0], 420, 6), ("Fresh Fruit Cake 500g", CATS[0], 480, 4), ("Red Velvet Cake 500g", CATS[0], 520, 4),
    ("Mango Cake 500g", CATS[0], 450, 5), ("Chocolate Truffle Cake 1kg", CATS[0], 880, 3), ("Black Forest Cake 1kg", CATS[0], 780, 3),
    ("Butterscotch Pastry", CATS[1], 60, 18), ("Strawberry Pastry", CATS[1], 60, 16), ("Red Velvet Pastry", CATS[1], 75, 12),
    ("Dutch Truffle Pastry", CATS[1], 80, 14), ("Fruit Pastry", CATS[1], 65, 10),
    ("Chicken Puff", CATS[2], 40, 18), ("Egg Puff", CATS[2], 30, 20), ("Cheese Sandwich", CATS[2], 50, 12),
    ("Veg Burger", CATS[2], 45, 10), ("Samosa Roll", CATS[2], 28, 15), ("Pizza Slice", CATS[2], 55, 8),
    ("Brown Bread", CATS[3], 50, 12), ("Fruit Bun", CATS[3], 20, 25), ("Cream Roll", CATS[3], 25, 20),
    ("Doughnut", CATS[3], 40, 15), ("Muffin", CATS[3], 35, 18), ("Rusk (200g)", CATS[3], 45, 14),
    ("Choco Chip Cookies 200g", CATS[4], 100, 12), ("Jeera Biscuits 200g", CATS[4], 70, 15), ("Plum Cake Slice", CATS[4], 35, 20),
    ("Brownie", CATS[4], 50, 16), ("Chocolate Slab", CATS[4], 60, 10),
    ("Cake Knife", CATS[5], 15, 30), ("Party Popper", CATS[5], 25, 20), ("Balloon Pack", CATS[5], 40, 15),
]

# Additive migrations only: existing products, sales and stock are never dropped or overwritten.
SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
  id SERIAL PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
  price INTEGER NOT NULL CHECK (price >= 0), stock INTEGER NOT NULL CHECK (stock >= 0),
  available BOOLEAN NOT NULL DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS sales(
  id SERIAL PRIMARY KEY, bill_no TEXT UNIQUE NOT NULL, idem_key TEXT UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), total INTEGER NOT NULL,
  pay TEXT NOT NULL, received INTEGER, items JSONB NOT NULL);
CREATE SEQUENCE IF NOT EXISTS bill_seq START 1001;
ALTER TABLE products ADD COLUMN IF NOT EXISTS image TEXT;
ALTER TABLE sales ADD COLUMN IF NOT EXISTS payments JSONB;
CREATE TABLE IF NOT EXISTS stock_log(
  id SERIAL PRIMARY KEY, product_id INTEGER, product_name TEXT NOT NULL,
  change INTEGER NOT NULL, before_qty INTEGER NOT NULL, after_qty INTEGER NOT NULL,
  reason TEXT NOT NULL, ref TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='settings')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='settings' AND column_name='key')
  THEN ALTER TABLE settings RENAME TO settings_legacy; END IF;
END $$;
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS sample_sets(name TEXT PRIMARY KEY);
"""

PRINTER_DEFAULTS = {
    "shop_name": "Sweet Counter", "address": "", "phone": "", "tax_id": "",
    "footer": "Thank you! Please visit again.", "paper_mm": 58, "font_px": 12,
    "show_demo_note": True, "auto_print": False,
}


def db():
    if CFG is None:
        raise RuntimeError("Settings not loaded")
    return psycopg.connect(CFG.database_url, row_factory=dict_row, connect_timeout=5)


def seed(c):
    with c.cursor() as cur:
        cur.executemany("INSERT INTO products(name,category,price,stock) VALUES(%s,%s,%s,%s)", SEED)


def add_missing_samples(c):
    added = []
    have = {r["name"].strip().lower() for r in c.execute("SELECT name FROM products").fetchall()}
    for name, cat, price, stock in SEED:
        if name.lower() in have:
            continue
        r = c.execute("INSERT INTO products(name,category,price,stock) VALUES(%s,%s,%s,%s) RETURNING *", (name, cat, price, stock)).fetchone()
        log_stock(c, r["id"], r["name"], 0, r["stock"], "opening")
        added.append(name)
    return added


def environment_guard(c, mode):
    """A database is either 'demo' or 'production', recorded in settings('environment').

    Stops a demo-mode deploy from touching a real outlet's database (and a production deploy from running on a
    demo one). An unmarked empty database adopts the current mode; an unmarked database that already holds data is
    only adopted by production mode (it is the existing trial data). To adopt it as demo instead, see docs/PHASE0.md.
    """
    row = c.execute("SELECT value FROM settings WHERE key='environment'").fetchone()
    marker = row["value"] if row else None
    if marker not in (None, "demo", "production"):
        raise ConfigError(f"settings.environment has an unexpected value {marker!r}; refusing to start")
    if marker and marker != mode:
        raise ConfigError(f"This database is marked {marker!r} but APP_MODE={mode!r}. Refusing to start so demo and "
                          "real data can never be mixed. Point DATABASE_URL at the right database.")
    if marker is None:
        has_data = c.execute("SELECT (SELECT count(*) FROM products) + (SELECT count(*) FROM sales) AS n").fetchone()["n"]
        if mode == "demo" and has_data:
            raise ConfigError("APP_MODE=demo refused: this database already has products/sales but is not marked as a "
                              "demo database. If it really is a throwaway demo DB, mark it explicitly (docs/PHASE0.md).")
        c.execute("INSERT INTO settings(key,value) VALUES('environment',%s) ON CONFLICT (key) DO NOTHING", (Json(mode),))
        log.warning("database marked as %s (existing rows: %s)", mode, has_data)


def demo_only(request: Request):
    """Destructive demo tools do not exist in production: 404, not 'forbidden', so they are not even discoverable."""
    if not CFG.is_demo:
        raise HTTPException(404, "Not found")


def assert_demo_db(c):
    row = c.execute("SELECT value FROM settings WHERE key='environment'").fetchone()
    if not row or row["value"] != "demo":  # second check, inside the transaction, right before anything destructive
        raise HTTPException(403, "This database is not marked as a demo database")


@asynccontextmanager
async def lifespan(_):
    global CFG
    CFG = load_settings(os.environ)  # raises ConfigError -> process exits before serving anything
    with db() as c:
        c.execute(SCHEMA)  # additive, IF NOT EXISTS only (unchanged from the prototype); never drops or rewrites data
        environment_guard(c, CFG.mode)
        if CFG.is_demo and c.execute("SELECT count(*) AS n FROM products").fetchone()["n"] == 0:
            seed(c)  # demo databases only; a production database is never seeded
    if CFG.is_demo:
        try:  # one-time top-up of extra sample products; must never stop the app from starting
            with db() as c:
                if not c.execute("SELECT 1 FROM sample_sets WHERE name='v2'").fetchone():
                    add_missing_samples(c)
                    c.execute("INSERT INTO sample_sets(name) VALUES('v2')")
        except Exception as e:
            log.warning("sample top-up skipped: %s", e)
    yield


_demo = os.environ.get("APP_MODE", "").strip().lower() == "demo"
# Interactive API docs list every endpoint; only expose them on a demo deployment.
app = FastAPI(title="Rush Counter POS", lifespan=lifespan,
              docs_url="/docs" if _demo else None, redoc_url=None, openapi_url="/openapi.json" if _demo else None)


def require(*roles):
    """Server-side permission check used by every protected endpoint. Hiding buttons in the UI is NOT the control."""
    def dep(request: Request) -> dict:
        sess = auth.read_session(CFG.session_secret, request.cookies.get(auth.COOKIE))
        if not sess:
            raise HTTPException(401, "Please sign in")
        if sess["role"] not in roles:
            raise HTTPException(403, "This account is not allowed to do that")
        if request.method not in ("GET", "HEAD", "OPTIONS"):  # CSRF: cookie alone is never enough to change data
            if not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), sess["csrf"].encode()):
                raise HTTPException(403, "Security check failed - reload the page and try again")
        return sess
    return dep


demo_only_dep = Depends(demo_only)
OWNER = Depends(require("owner"))
ANY = Depends(require("owner", "staff"))


class LoginIn(BaseModel):
    role: Literal["owner", "staff"]
    password: str = Field(min_length=1, max_length=200)


@app.middleware("http")
async def headers(request: Request, call_next):
    r = await call_next(request)
    r.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/api/"):
        r.headers["Cache-Control"] = "no-store"  # never let a browser/proxy cache sales or stock responses
    return r


@app.get("/api/session")
def session(request: Request):
    """Public and harmless: lets the page decide whether to show the login screen."""
    sess = auth.read_session(CFG.session_secret, request.cookies.get(auth.COOKIE))
    if not sess:
        return {"authenticated": False, "mode": CFG.mode, "staff_login": bool(CFG.staff_hash)}
    return {"authenticated": True, "role": sess["role"], "csrf": sess["csrf"], "mode": CFG.mode}


@app.post("/api/login")
def login(b: LoginIn, response: Response):
    wait = THROTTLE.retry_after(b.role)
    if wait:
        raise HTTPException(429, f"Too many wrong attempts. Try again in about {wait // 60 + 1} min.",
                            headers={"Retry-After": str(wait)})
    ok = auth.verify_password(b.password, CFG.owner_hash if b.role == "owner" else CFG.staff_hash)
    if not ok:
        THROTTLE.failure(b.role)
        log.warning("login failed role=%s", b.role)  # never log the password
        raise HTTPException(401, "Wrong password")
    THROTTLE.success(b.role)
    token = auth.make_session(CFG.session_secret, b.role, CFG.session_hours)
    response.set_cookie(auth.COOKIE, token, max_age=CFG.session_hours * 3600, httponly=True,
                        secure=CFG.cookie_secure, samesite="strict", path="/")
    sess = auth.read_session(CFG.session_secret, token)
    log.info("login ok role=%s", b.role)
    return {"authenticated": True, "role": b.role, "csrf": sess["csrf"], "mode": CFG.mode}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE, path="/", secure=CFG.cookie_secure, httponly=True, samesite="strict")
    return {"ok": True}


class ProductEdit(BaseModel):
    """Fields an edit may change. Stock is deliberately NOT here: stock only changes through /stock (delta, logged)
    or a sale, so a stale browser copy can never overwrite it. Unknown fields (e.g. an old client's `stock`) are ignored."""
    name: str = Field(min_length=1, max_length=120)
    cat: str = Field(min_length=1, max_length=60)
    price: int = Field(ge=0, le=1_000_000)
    on: bool = True
    image: Optional[str] = Field(default=None, max_length=500)

    @field_validator("image")
    @classmethod
    def _img(cls, v):
        v = (v or "").strip()
        if not v:
            return None
        if not re.match(r"^(https?://|/)\S+$", v):
            raise ValueError("Image must be an http(s) link or a /path")
        return v


class ProductIn(ProductEdit):
    stock: int = Field(ge=0, le=1_000_000)  # opening stock, only when CREATING a product


class AvailabilityIn(BaseModel):
    on: bool


class Line(BaseModel):
    id: int
    q: int = Field(ge=1, le=999)


class SaleIn(BaseModel):
    key: str = Field(min_length=8, max_length=80)  # idempotency key: required, one per bill attempt
    items: List[Line] = Field(min_length=1)
    pay: Literal["Cash", "UPI", "Card", "Split"]  # recorded only; no real payment is processed
    recv: Optional[int] = Field(default=None, ge=0)
    cash: Optional[int] = Field(default=None, ge=0)  # Split only
    upi: Optional[int] = Field(default=None, ge=0)   # Split only


class StockIn(BaseModel):
    add: Optional[int] = Field(default=None, ge=-1_000_000, le=1_000_000)
    set_to: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    expected: Optional[int] = Field(default=None, ge=0, le=1_000_000)  # stock the person saw when counting (set_to only)
    note: Optional[str] = Field(default=None, max_length=60)


class PrinterIn(BaseModel):
    shop_name: str = Field(min_length=1, max_length=40)
    address: str = Field(default="", max_length=120)
    phone: str = Field(default="", max_length=30)
    tax_id: str = Field(default="", max_length=30)
    footer: str = Field(default="", max_length=120)
    paper_mm: Literal[58, 80] = 58
    font_px: int = Field(default=12, ge=10, le=18)
    show_demo_note: bool = True
    auto_print: bool = False


def prod_out(r):
    return {"id": r["id"], "name": r["name"], "cat": r["category"], "price": r["price"],
            "stock": r["stock"], "on": r["available"], "image": r.get("image")}


def sale_out(r):
    pays = r.get("payments") or {r["pay"]: r["total"]}
    return {"no": r["bill_no"], "t": int(r["created_at"].timestamp() * 1000), "items": r["items"],
            "total": r["total"], "pay": r["pay"], "recv": r["received"], "payments": pays}


def log_stock(c, pid, name, before, after, reason, ref=None):
    if before != after:
        c.execute("INSERT INTO stock_log(product_id,product_name,change,before_qty,after_qty,reason,ref) "
                  "VALUES(%s,%s,%s,%s,%s,%s,%s)", (pid, name, after - before, before, after, reason, ref))


def get_printer(c):
    row = c.execute("SELECT value FROM settings WHERE key='printer'").fetchone()
    return {**PRINTER_DEFAULTS, **(row["value"] if row else {})}


@app.get("/healthz")
def healthz():
    with db() as c:
        c.execute("SELECT 1")
    return {"ok": True}


@app.get("/api/state")
def state(sess: dict = ANY):
    # Owners get full history (as before). Counter staff get only the latest few bills: enough to reprint, not a sales ledger.
    n = 2000 if sess["role"] == "owner" else STAFF_RECENT_BILLS
    with db() as c:
        prods = c.execute("SELECT * FROM products ORDER BY id").fetchall()
        sales = c.execute("SELECT * FROM (SELECT * FROM sales ORDER BY id DESC LIMIT %s) t ORDER BY id", (n,)).fetchall()
        settings = get_printer(c)
    cats = list(dict.fromkeys(p["category"] for p in prods))  # categories come from the data, in first-seen order
    return {"products": [prod_out(p) for p in prods], "sales": [sale_out(s) for s in sales],
            "categories": cats, "printer": settings, "role": sess["role"], "mode": CFG.mode}


@app.post("/api/products", dependencies=[OWNER])
def add_product(p: ProductIn):
    with db() as c:
        r = c.execute("INSERT INTO products(name,category,price,stock,available,image) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
                      (p.name.strip(), p.cat.strip(), p.price, p.stock, p.on, p.image)).fetchone()
        log_stock(c, r["id"], r["name"], 0, r["stock"], "opening")
    return prod_out(r)


@app.put("/api/products/{pid}", dependencies=[OWNER])
def edit_product(pid: int, p: ProductEdit):
    with db() as c:
        old = c.execute("SELECT * FROM products WHERE id=%s FOR UPDATE", (pid,)).fetchone()
        if not old:
            raise HTTPException(404, "Product not found")
        r = c.execute("UPDATE products SET name=%s,category=%s,price=%s,available=%s,image=%s WHERE id=%s RETURNING *",
                      (p.name.strip(), p.cat.strip(), p.price, p.on, p.image, pid)).fetchone()
    return prod_out(r)


@app.patch("/api/products/{pid}/availability", dependencies=[OWNER])
def set_availability(pid: int, b: AvailabilityIn):
    """Sold-out / available toggle. Touches ONLY `available`; sends an explicit target state so a repeat is harmless."""
    with db() as c:
        r = c.execute("UPDATE products SET available=%s WHERE id=%s RETURNING *", (b.on, pid)).fetchone()
        if not r:
            raise HTTPException(404, "Product not found")
    return prod_out(r)


@app.post("/api/products/{pid}/stock", dependencies=[OWNER])
def adjust_stock(pid: int, b: StockIn):
    """Quick replenishment (add) or correction (set_to). Logged; never goes below zero."""
    if (b.add is None) == (b.set_to is None):
        raise HTTPException(422, "Send either add or set_to")
    with db() as c:
        r = c.execute("SELECT * FROM products WHERE id=%s FOR UPDATE", (pid,)).fetchone()
        if not r:
            raise HTTPException(404, "Product not found")
        if b.set_to is not None and b.expected is not None and b.expected != r["stock"]:
            raise HTTPException(409, f"Stock changed while you were counting: it is now {r['stock']}, not {b.expected}. "
                                     "Please recount.")
        new = r["stock"] + b.add if b.add is not None else b.set_to
        if new < 0:
            raise HTTPException(409, f"Only {r['stock']} in stock - cannot remove {-b.add}")
        reason = "restock" if (b.add or 0) > 0 else "correction"
        r2 = c.execute("UPDATE products SET stock=%s WHERE id=%s RETURNING *", (new, pid)).fetchone()
        log_stock(c, pid, r["name"], r["stock"], new, reason, (b.note or "").strip() or None)
    return prod_out(r2)


@app.get("/api/stock-log", dependencies=[OWNER])
def stock_log(limit: int = 40):
    limit = max(1, min(limit, 200))
    with db() as c:
        rows = c.execute("SELECT * FROM stock_log ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return [{"id": r["id"], "name": r["product_name"], "change": r["change"], "after": r["after_qty"],
             "reason": r["reason"], "ref": r["ref"], "t": int(r["created_at"].timestamp() * 1000)} for r in rows]


@app.put("/api/printer", dependencies=[OWNER])
def save_printer(b: PrinterIn):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES('printer',%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                  (Json(b.model_dump()),))
    return b.model_dump()


def _create_sale(b: SaleIn):
    qty = requested_qty([{"id": i.id, "q": i.q} for i in b.items])
    with db() as c:
        # Serialise every request that carries the same key BEFORE looking it up. Without this, a retry arriving while
        # the first attempt is still committing would pass the lookup, then fail the stock check (the first attempt
        # already took the stock) and show a bogus "only N in stock" error for a sale that actually succeeded.
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (b.key,))
        old = c.execute("SELECT * FROM sales WHERE idem_key=%s", (b.key,)).fetchone()
        if old:
            if not same_request(old["items"], old["pay"], old.get("payments"), qty, b.pay, b.cash, b.upi):
                raise HTTPException(409, "This bill reference was already used for a different bill. "
                                         "Clear the bill and ring it up again.")
            return sale_out(old)  # same request sent twice: return the original bill, never a duplicate (stock untouched)
        rows = {r["id"]: r for r in c.execute(
            "SELECT * FROM products WHERE id = ANY(%s) ORDER BY id FOR UPDATE", (list(qty),))}
        items, total = [], 0
        for pid, q in qty.items():
            r = rows.get(pid)
            if not r:
                raise HTTPException(409, "A product on this bill no longer exists")
            if not r["available"]:
                raise HTTPException(409, f"{r['name']} is unavailable")
            if r["stock"] < q:
                raise HTTPException(409, f"Only {r['stock']} of {r['name']} in stock")
            items.append({"id": pid, "name": r["name"], "q": q, "price": r["price"], "before": r["stock"]})
            total += r["price"] * q
        if b.pay == "Cash" and b.recv is not None and b.recv < total:
            raise HTTPException(400, "Cash received is less than total")
        if b.pay == "Split":
            if b.cash is None or b.upi is None or b.cash + b.upi != total:
                raise HTTPException(400, "Split amounts must add up to the bill total")
            payments = {k: v for k, v in (("Cash", b.cash), ("UPI", b.upi)) if v > 0}
        else:
            payments = {b.pay: total}
        no = "DEMO-%d" % c.execute("SELECT nextval('bill_seq') AS n").fetchone()["n"]
        for it in items:  # stock leaves only here, once, in the same transaction as the bill
            c.execute("UPDATE products SET stock = stock - %s WHERE id=%s", (it["q"], it["id"]))
            log_stock(c, it["id"], it["name"], it["before"], it["before"] - it["q"], "sale", no)
            del it["before"]
        r = c.execute("INSERT INTO sales(bill_no,idem_key,total,pay,received,items,payments) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                      (no, b.key, total, b.pay, b.recv if b.pay == "Cash" else None, Json(items), Json(payments))).fetchone()
        return sale_out(r)


@app.post("/api/sales", dependencies=[ANY])
def create_sale(b: SaleIn):
    try:
        return _create_sale(b)
    except psycopg.errors.UniqueViolation:  # two identical requests raced; return the winner
        with db() as c:
            old = c.execute("SELECT * FROM sales WHERE idem_key=%s", (b.key,)).fetchone()
        if old:
            return sale_out(old)
        raise HTTPException(409, "Please try again")


@app.get("/api/sales/by-key/{key}", dependencies=[ANY])
def sale_by_key(key: str):
    """Lets the counter ask 'did my earlier attempt get saved?' after a dropped connection (404 = it did not)."""
    with db() as c:
        old = c.execute("SELECT * FROM sales WHERE idem_key=%s", (key[:80],)).fetchone()
    if not old:
        raise HTTPException(404, "No sale with that reference")
    return sale_out(old)


@app.post("/api/restore-products", dependencies=[demo_only_dep, OWNER])
def restore_products():
    """Additive only: adds any sample product that is missing (matched by name). Existing products, stock and sales are untouched."""
    with db() as c:
        assert_demo_db(c)
        return {"added": add_missing_samples(c)}


@app.post("/api/reset", dependencies=[demo_only_dep, OWNER])
def reset():
    """Demo-only: wipes products, sales and the stock log, then re-seeds samples. Printer settings are kept."""
    with db() as c:
        assert_demo_db(c)
        c.execute("TRUNCATE sales, products, stock_log RESTART IDENTITY")
        c.execute("ALTER SEQUENCE bill_seq RESTART WITH 1001")
        seed(c)
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/", StaticFiles(directory=STATIC), name="static")
