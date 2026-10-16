"""Rush Counter POS (independent concept demo) - FastAPI + PostgreSQL."""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/rushpos")
STATIC = Path(__file__).resolve().parent.parent / "static"
CATS = ["Cakes", "Pastries", "Puffs & Savouries", "Bread & Bakery", "Cookies & Chocolates", "Candles & Birthday Accessories"]

# Sample data only - not official Monginis prices.
SEED = [
    ("Chocolate Truffle Cake 500g", CATS[0], 450, 8), ("Black Forest Cake 500g", CATS[0], 400, 6),
    ("Pineapple Cake 500g", CATS[0], 380, 5), ("Black Forest Pastry", CATS[1], 60, 24),
    ("Pineapple Pastry", CATS[1], 55, 20), ("Chocolate Pastry", CATS[1], 65, 18),
    ("Veg Puff", CATS[2], 25, 30), ("Paneer Puff", CATS[2], 35, 20), ("Veg Patty", CATS[2], 30, 3),
    ("Sandwich Bread", CATS[3], 45, 15), ("Pav (6 pcs)", CATS[3], 30, 12), ("Cupcake", CATS[3], 30, 25),
    ("Butter Cookies 200g", CATS[4], 90, 14), ("Choco Bar", CATS[4], 40, 0),
    ("Birthday Candles", CATS[5], 20, 40), ("Number Candle", CATS[5], 30, 15), ("Party Cap (pack)", CATS[5], 50, 10),
]

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
"""


def db():
    return psycopg.connect(DB_URL, row_factory=dict_row)


def seed(c):
    with c.cursor() as cur:
        cur.executemany("INSERT INTO products(name,category,price,stock) VALUES(%s,%s,%s,%s)", SEED)


@asynccontextmanager
async def lifespan(_):
    with db() as c:
        c.execute(SCHEMA)
        if c.execute("SELECT count(*) AS n FROM products").fetchone()["n"] == 0:
            seed(c)
    yield


app = FastAPI(title="Rush Counter POS demo", lifespan=lifespan)


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    cat: str
    price: int = Field(ge=0, le=1_000_000)
    stock: int = Field(ge=0, le=1_000_000)
    on: bool = True


class Line(BaseModel):
    id: int
    q: int = Field(ge=1, le=999)


class SaleIn(BaseModel):
    key: Optional[str] = Field(default=None, max_length=80)
    items: List[Line] = Field(min_length=1)
    pay: Literal["Cash", "UPI", "Card"]  # recorded only; no real payment is processed
    recv: Optional[int] = Field(default=None, ge=0)


def prod_out(r):
    return {"id": r["id"], "name": r["name"], "cat": r["category"], "price": r["price"], "stock": r["stock"], "on": r["available"]}


def sale_out(r):
    return {"no": r["bill_no"], "t": int(r["created_at"].timestamp() * 1000), "items": r["items"],
            "total": r["total"], "pay": r["pay"], "recv": r["received"]}


@app.get("/healthz")
def healthz():
    with db() as c:
        c.execute("SELECT 1")
    return {"ok": True}


@app.get("/api/state")
def state():
    """Initial page data. Avoid calling this endpoint after each sale."""
    with db() as c:
        prods = c.execute("SELECT * FROM products ORDER BY id").fetchall()
        sales = c.execute("SELECT * FROM sales ORDER BY id").fetchall()
    return {"products": [prod_out(p) for p in prods], "sales": [sale_out(s) for s in sales]}


@app.get("/api/products")
def products():
    """Lightweight stock refresh for conflict recovery and inventory tools."""
    with db() as c:
        prods = c.execute("SELECT * FROM products ORDER BY id").fetchall()
    return [prod_out(p) for p in prods]


def check_cat(cat):
    if cat not in CATS:
        raise HTTPException(422, "Unknown category")


@app.post("/api/products")
def add_product(p: ProductIn):
    check_cat(p.cat)
    with db() as c:
        r = c.execute("INSERT INTO products(name,category,price,stock,available) VALUES(%s,%s,%s,%s,%s) RETURNING *",
                      (p.name.strip(), p.cat, p.price, p.stock, p.on)).fetchone()
    return prod_out(r)


@app.put("/api/products/{pid}")
def edit_product(pid: int, p: ProductIn):
    check_cat(p.cat)
    with db() as c:
        r = c.execute("UPDATE products SET name=%s,category=%s,price=%s,stock=%s,available=%s WHERE id=%s RETURNING *",
                      (p.name.strip(), p.cat, p.price, p.stock, p.on, pid)).fetchone()
    if not r:
        raise HTTPException(404, "Product not found")
    return prod_out(r)


def _sale_payload(c, sale_row):
    """Return a bill and authoritative stock values in the same round-trip."""
    result = sale_out(sale_row)
    product_ids = [int(item["id"]) for item in sale_row["items"]]
    stock_rows = c.execute(
        "SELECT id, stock FROM products WHERE id = ANY(%s)", (product_ids,)
    ).fetchall()
    result["stocks"] = {str(row["id"]): row["stock"] for row in stock_rows}
    return result


def _create_sale(b: SaleIn):
    qty = {}
    for i in b.items:
        qty[i.id] = qty.get(i.id, 0) + i.q
    with db() as c:
        if b.key:
            old = c.execute("SELECT * FROM sales WHERE idem_key=%s", (b.key,)).fetchone()
            if old:  # a retry returns the original bill and fresh stock values
                return _sale_payload(c, old)
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
            items.append({"id": pid, "name": r["name"], "q": q, "price": r["price"]})
            total += r["price"] * q
        if b.pay == "Cash" and b.recv is not None and b.recv < total:
            raise HTTPException(400, "Cash received is less than total")
        for it in items:
            c.execute("UPDATE products SET stock = stock - %s WHERE id=%s", (it["q"], it["id"]))
        no = "DEMO-%d" % c.execute("SELECT nextval('bill_seq') AS n").fetchone()["n"]
        r = c.execute("INSERT INTO sales(bill_no,idem_key,total,pay,received,items) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
                      (no, b.key, total, b.pay, b.recv if b.pay == "Cash" else None, Json(items))).fetchone()
        return _sale_payload(c, r)


@app.post("/api/sales")
def create_sale(b: SaleIn):
    try:
        return _create_sale(b)
    except psycopg.errors.UniqueViolation:  # two identical requests raced; return the winner
        with db() as c:
            old = c.execute("SELECT * FROM sales WHERE idem_key=%s", (b.key,)).fetchone()
            if old:
                return _sale_payload(c, old)
        raise HTTPException(409, "Please try again")


@app.post("/api/reset")
def reset():
    with db() as c:
        c.execute("TRUNCATE sales, products RESTART IDENTITY")
        c.execute("ALTER SEQUENCE bill_seq RESTART WITH 1001")
        seed(c)
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/", StaticFiles(directory=STATIC), name="static")
