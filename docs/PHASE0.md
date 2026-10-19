# Phase 0 safety patch — read before deploying

Scope: stop the prototype from being dangerous on a real outlet. **No schema changes, no multi-outlet work, no hosting change.**
This does **not** make the app production-ready (see "Verification still required").

## What changed (one git commit per item: `git log --oneline`)
| Finding | Where it was | Fix |
|---|---|---|
| Open by default / PIN | `main.py` `owner()`, unprotected `/api/state`, `/api/sales` | `require()` role check on every route; signed session cookie; CSRF header; login throttle |
| Reset / sample data in production | `lifespan` seeding, `/api/reset`, `/api/restore-products` | Only with `APP_MODE=demo` **and** DB marker `settings.environment = "demo"`; otherwise 404 |
| Silent localhost fallback | `DB_URL = environ.get(..., localhost)` | `app/config.py` refuses to start, listing every missing/insecure value |
| Stale-stock overwrite | `PUT /api/products` wrote `stock`; `tog()` sent cached stock | Edits cannot carry stock; availability has its own `PATCH`; "Set to" is rejected if stock moved (`expected`) |
| Retry race | idempotency lookup before locks | `pg_advisory_xact_lock(key)` first; same key + different bill → 409; `GET /api/sales/by-key` |
| False "No connection" after every sale | `pay()`: local `const sig` shadowed global `sig()` | renamed; real code now covered by `tests/js/pay.test.js` |

Also: `/docs` and `/openapi.json` only in demo mode; `Cache-Control: no-store` on `/api/*`; 20 s request timeout in the browser; DB connect timeout 5 s.

## Roles
* **owner**: everything. **staff** (counter): sign in, ring sales, see availability, last 30 bills (reprint). No stock edits, products, printer settings, totals or history.
* Two **shared** accounts (hashes in env vars) because a users table would be a schema change. Consequence: logs say *what* happened, not *which person*. Fixed by per-user accounts in Phase 2.

## Required environment variables
| Var | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | no default |
| `SESSION_SECRET` | yes | ≥32 chars. Rotating it signs everyone out |
| `OWNER_PASSWORD_HASH` | yes | `python scripts/hash_password.py` |
| `STAFF_PASSWORD_HASH` | no | empty = counter login disabled |
| `APP_MODE` | no | `production` (default) or `demo` |
| `SESSION_HOURS` | no | 1–24, default 12 |
| `COOKIE_SECURE` | no | `false` only with `APP_MODE=demo` |
`OWNER_PIN` is gone.

## PROPOSED deployment change — NOT applied (render.yaml untouched)
```diff
       - key: OWNER_PIN   # optional ...
-        sync: false
+      - key: APP_MODE
+        value: production
+      - key: SESSION_SECRET
+        sync: false
+      - key: OWNER_PASSWORD_HASH
+        sync: false
+      - key: STAFF_PASSWORD_HASH
+        sync: false
```
Set the three secrets in the Render dashboard **before** deploying. Plans stay as they are until you decide; see "Hosting" below.

## Schema / data impact
* **No DDL added.** The existing additive `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` startup block is unchanged (it still runs every boot; proper migrations are Phase 1).
* **One row written on first start:** `settings('environment')` = `"production"` or `"demo"`. Old code ignores it.
* **Do not point production mode at the prototype database.** It contains sample products and `DEMO-` bills; production mode would adopt and mark it `production`. Create a new empty database for the real outlet. To keep the old DB as a demo instead, mark it explicitly first:
  `INSERT INTO settings(key,value) VALUES('environment','"demo"') ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value;`
  then run that deployment with `APP_MODE=demo`.

## Risks of deploying this patch
1. Missing env var → app refuses to start (intended). On Render the previous deploy should stay live if the new one fails its health check — confirm in your dashboard rather than assume.
2. API contract changes: login required; `key` required on `POST /api/sales`; `PUT /api/products/{id}` ignores `stock`; `pin_required`/`X-Owner-Pin` removed. A tablet with the old page cached must reload.
3. Everyone is signed out on deploy; staff need the counter password.
4. Forgotten password = change the env var and redeploy.

## Rollback
No DB rollback is needed. Redeploy the previous Render deploy, or `git revert` to baseline `262add5`. **Rolling back re-opens the P0 holes** (open reset, unauthenticated sales history): use it only as a short stop-gap, and never with real data. Optional cleanup: `DELETE FROM settings WHERE key='environment'`.

## Hosting (decision pending, nothing changed)
Render's docs state free Postgres expires after 30 days and has no backups, and free web services spin down when idle. The backup scripts reduce but do not remove that risk. **Do not keep real sales on the free database.** Re-verify current plan terms on Render's pricing page before choosing.

## Remaining races and limitations (honest list)
* **Page reload mid-checkout:** the open bill and its idempotency key live in browser memory. If the response was lost *and* the page is reloaded, re-ringing creates a second sale. Needs a persisted open bill (Phase 4) and a void flow.
* **Human double-ring:** a cashier who re-rings an order they believe failed (different key) is a process risk the software cannot detect.
* **Sessions are stateless:** "Sign out" clears that device only; a copied cookie stays valid until expiry (≤ `SESSION_HOURS`). Rotate `SESSION_SECRET` to revoke all.
* **Login throttle** is in memory, per process, per role (not per IP): resets on restart; an attacker can lock the login for 5 min (nuisance, not a breach).
* **Concurrent owner edits** to name/price/category are last-write-wins. Price/name changes are not audited (Phase 3).
* **Bill numbers** still come from one global sequence (rolled-back transactions can leave gaps) and still start `DEMO-`. Changing that alters the bill format — **your decision**.
* Receipt still prints "Concept demo" text unless turned off in Printer settings; footer note in the UI remains.
* `/healthz` still checks the database (a DB blip makes Render think the app is down).
* No per-user attribution in `stock_log`.

## Verification
Executed in the authoring sandbox: `python -m unittest discover -s tests` (22 tests: config, auth, idempotency rules) and `node --test tests/js/pay.test.js` (7 tests, runs the real `pay()`); JS and Python syntax checks; the refusal paths of both backup scripts with stub tools.
**Written but NOT executed** (no PostgreSQL/FastAPI available there): `tests/test_api_integration.py` (21 tests: permissions matrix, CSRF, throttle, production isolation, stale stock, idempotency, concurrent last-item sale); the real `pg_dump`/`pg_restore` paths of the backup scripts; the browser UI as a whole.

**Still required before any "production-ready" claim:** (1) run the integration tests on a throwaway PostgreSQL and fix what they find; (2) run `backup.sh` then `restore_verify.sh` against a real database; (3) click through login, counter, stock and printer screens on the actual tablet; (4) print on the actual printer; (5) a paid, backed-up database decision; (6) an external look at the auth design.
