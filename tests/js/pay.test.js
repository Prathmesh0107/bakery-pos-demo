// Executes the REAL paid()/pay() source from static/index.html against stubs.  Run:  node --test tests/js
const test = require('node:test'), assert = require('node:assert'), fs = require('fs'), vm = require('vm'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const js = html.match(/<script>([\s\S]*)<\/script>/)[1];
const src = js.match(/function paid\(s\)\{[\s\S]*?\n(?=\/\* ---------- receipt)/)[0];

function world(apiImpl, cart = [{ id: 1, q: 2 }]) {
  const calls = [], toasts = [], modals = [];
  const ctx = {
    cart, busy: false, ck: null, hist: [], lastSale: null, view: 'rush', S: { sales: [], printer: { auto_print: false } },
    P: () => ({ stock: 9 }), cartR() {}, closeBill() {}, top_() {}, prodR() {}, showLast() {}, printReceipt() {}, fixCart() {},
    load: async () => true, sig: () => 'state', closeM() {}, esc: x => x, M: n => '₹' + n,
    toast: (t, e) => toasts.push({ t, e: !!e }), modal: h => modals.push(h), crypto: { randomUUID: (() => { let n = 0; return () => 'key-' + (++n); })() },
    JSON, Math, Date, encodeURIComponent,
    api: async (p, o) => { calls.push({ p, body: o && o.body ? JSON.parse(o.body) : null }); return apiImpl(p, o, calls); },
  };
  vm.createContext(ctx); vm.runInContext(src, ctx);
  return { ctx, calls, toasts, modals, pay: m => vm.runInContext(`pay(${JSON.stringify(m)})`, ctx) };
}
const sale = (no = 'DEMO-1001') => ({ no, total: 100, pay: 'Cash', items: [{ id: 1, q: 2, name: 'x', price: 50 }], t: Date.now() });
const netErr = () => { throw new TypeError('Failed to fetch'); };
const httpErr = (status, msg = 'boom') => { const e = new Error(msg); e.status = status; throw e; };

test('successful sale: success message, no error toast, bill cleared', async () => {
  const w = world(async () => sale()); await w.pay('Cash');
  assert.deepStrictEqual(w.toasts.filter(t => t.e), []);
  assert.ok(w.toasts.some(t => /Sale saved/.test(t.t)));
  assert.strictEqual(JSON.stringify(w.ctx.cart), '[]'); assert.strictEqual(w.ctx.ck, null); assert.strictEqual(w.ctx.S.sales.length, 1);
});

test('no answer, retry with the SAME bill reuses the SAME idempotency key', async () => {
  let n = 0; const w = world(async () => (++n === 1 ? netErr() : sale()));
  await w.pay('Cash'); assert.strictEqual(w.ctx.cart.length, 1); assert.strictEqual(w.ctx.ck.unsure, true);
  await w.pay('Cash');
  const posts = w.calls.filter(c => c.p === '/sales'); assert.strictEqual(posts.length, 2);
  assert.strictEqual(posts[0].body.key, posts[1].body.key); assert.strictEqual(JSON.stringify(w.ctx.cart), '[]');
});

test('no answer, bill edited, earlier attempt WAS saved: no second sale is created', async () => {
  const w = world(async (p) => { if (p === '/sales') return netErr(); return sale('DEMO-1005'); });
  await w.pay('Cash'); w.ctx.cart = [{ id: 1, q: 3 }];            // staff change the bill after the failure
  await w.pay('Cash');
  assert.strictEqual(w.calls.filter(c => c.p === '/sales').length, 1, 'must not POST a second sale');
  assert.ok(w.calls.some(c => c.p.startsWith('/sales/by-key/key-1')));
  assert.strictEqual(w.ctx.cart.length, 1, 'the edited open bill must be kept, not discarded');
  assert.ok(w.modals.some(m => /already saved/.test(m)));
});

test('no answer, bill edited, earlier attempt was NOT saved (404): new key, sale goes through', async () => {
  const w = world(async (p) => { if (p === '/sales' && !w_done.v) { w_done.v = 1; return netErr(); } if (p.startsWith('/sales/by-key')) return httpErr(404); return sale(); });
  const w_done = { v: 0 }; await w.pay('Cash'); w.ctx.cart = [{ id: 1, q: 3 }]; await w.pay('Cash');
  const posts = w.calls.filter(c => c.p === '/sales'); assert.strictEqual(posts.length, 2); assert.notStrictEqual(posts[0].body.key, posts[1].body.key);
  assert.strictEqual(JSON.stringify(w.ctx.cart), '[]');
});

test('double-tap while a checkout is in flight sends exactly one request', async () => {
  let release; const gate = new Promise(r => (release = r));
  const w = world(async () => { await gate; return sale(); });
  const a = w.pay('Cash'); const b = w.pay('Cash'); release(); await Promise.all([a, b]);
  assert.strictEqual(w.calls.filter(c => c.p === '/sales').length, 1);
});

test('definite refusal (409) is not treated as unknown and keeps the bill', async () => {
  const w = world(async () => httpErr(409, 'Only 1 of Veg Puff in stock')); await w.pay('Cash');
  assert.ok(w.toasts.some(t => t.e && /Only 1/.test(t.t))); assert.strictEqual(w.ctx.cart.length, 1); assert.ok(!w.ctx.ck.unsure);
});

test('server error (500) is treated as "unknown outcome"', async () => {
  const w = world(async () => httpErr(500)); await w.pay('Cash'); assert.strictEqual(w.ctx.ck.unsure, true); assert.strictEqual(w.ctx.busy, false);
});
