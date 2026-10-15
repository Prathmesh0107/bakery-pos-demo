# Rush Counter POS — concept demo (FastAPI + PostgreSQL)

Independent concept demo. **Not an official Monginis product**; no Monginis approval implied.
Prices, stock and sales are sample data. UPI/Card only *record* the chosen method; no real payment is processed.

## Deploy on Render
1. Push this folder to a GitHub repo.
2. Render dashboard -> **New -> Blueprint** -> pick the repo. `render.yaml` creates the web service and the Postgres database and wires `DATABASE_URL` automatically.
3. Open the service URL once it is live. Tables and sample products are created on first start.
4. Optional device viewer (phone/tablet frames for a laptop demo): add `?viewer=1` to the URL.

Free-tier notes (check Render's current terms): free web services sleep when idle (first load can take ~30-60 s, so open it before the demo), and free Postgres databases may expire after a limited period. Use a paid plan for anything long-lived.

## Run locally
```
docker run -d --name rushdb -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=rushpos -p 5432:5432 postgres:16
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rushpos
uvicorn app.main:app --reload
```
Open http://localhost:8000

## What the database does
- `products`, `sales` tables plus a `bill_seq` sequence for sequential bill numbers (DEMO-1001, ...).
- Completing a sale runs in one transaction: rows are locked, stock/availability are checked, stock is deducted, the bill is saved. Drafts never touch stock.
- Each checkout sends an idempotency key, so a double-click or retry returns the same bill instead of a duplicate.
- Data is shared by every device using the URL. Refresh another device to see new sales/stock.
- **Reset Demo Data** (page footer) truncates the two tables and re-seeds them.

## API
`GET /api/state` · `POST /api/products` · `PUT /api/products/{id}` · `POST /api/sales` · `POST /api/reset` · `GET /healthz`

Layout: `app/main.py` (API), `static/index.html` (UI), `render.yaml`, `requirements.txt`.
