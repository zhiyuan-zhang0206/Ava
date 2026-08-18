#!/usr/bin/env node
/* Ledger — test.js
 * Plain-Node test suite for store.js. No npm dependencies.
 * Run: node test.js   (exits non-zero on any failure)
 */
'use strict';

const assert = require('assert');
const S = require('./store.js');

let passed = 0;
let failed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('  ok    ' + name);
  } catch (err) {
    failed += 1;
    failures.push(name + ' -> ' + err.message);
    console.log('  FAIL  ' + name + '\n        ' + err.message);
  }
}

function section(name) {
  console.log('\n== ' + name + ' ==');
}

/* ------------------------- test helpers ------------------------- */

function fakeStorage(initial) {
  const data = new Map();
  if (initial !== undefined && initial !== null) data.set(S.STORAGE_KEY, String(initial));
  return {
    getItem(k) { return data.has(k) ? data.get(k) : null; },
    setItem(k, v) { data.set(k, String(v)); },
    removeItem(k) { data.delete(k); }
  };
}

function fakeTimers() {
  let now = 0;
  let seq = 0;
  const jobs = new Map();
  return {
    setTimeout(fn, ms) {
      const id = ++seq;
      jobs.set(id, { at: now + ms, fn });
      return id;
    },
    clearTimeout(id) { jobs.delete(id); },
    advance(ms) {
      now += ms;
      const due = [];
      jobs.forEach((job, id) => { if (job.at <= now) due.push([id, job]); });
      due.sort((a, b) => a[1].at - b[1].at);
      for (const [id, job] of due) {
        if (jobs.has(id)) { jobs.delete(id); job.fn(); }
      }
    },
    pending() { return jobs.size; }
  };
}

function firstCategoryId(st) {
  for (const c of st.getState().categories) {
    if (c.id !== S.UNCATEGORIZED_ID) return c.id;
  }
  return st.getState().categories[0].id;
}

function makeEntry(overrides) {
  return Object.assign({
    id: 'e' + Math.random().toString(36).slice(2, 10),
    type: 'expense',
    title: 'Item',
    amountCents: 100,
    date: '2026-08-13',
    categoryId: 'cat-food'
  }, overrides);
}

/* ------------------------------ money ------------------------------ */

section('Money: integer cents');

test('parseAmountToCents parses decimals, integers, and commas', () => {
  assert.strictEqual(S.parseAmountToCents('12.34'), 1234);
  assert.strictEqual(S.parseAmountToCents('1,234.56'), 123456);
  assert.strictEqual(S.parseAmountToCents('1234'), 123400);
  assert.strictEqual(S.parseAmountToCents('12.3'), 1230);
  assert.strictEqual(S.parseAmountToCents('0.01'), 1);
  assert.strictEqual(S.parseAmountToCents(' 45.50 '), 4550);
});

test('0.1 + 0.2 is exactly 30 cents — no float artifacts', () => {
  const a = S.parseAmountToCents('0.1');
  const b = S.parseAmountToCents('0.2');
  assert.strictEqual(a + b, 30);
  assert.strictEqual(S.formatCents(a + b), '0.30');
});

test('float-artifact strings are rejected', () => {
  assert.strictEqual(S.parseAmountToCents('0.30000000000000004'), null);
});

test('parseAmountToCents rejects junk', () => {
  assert.strictEqual(S.parseAmountToCents(''), null);
  assert.strictEqual(S.parseAmountToCents('abc'), null);
  assert.strictEqual(S.parseAmountToCents('-5'), null);
  assert.strictEqual(S.parseAmountToCents('0'), null);
  assert.strictEqual(S.parseAmountToCents('1.234'), null);
  assert.strictEqual(S.parseAmountToCents('12.'), null);
  assert.strictEqual(S.parseAmountToCents('1,23.45'), null);
});

test('formatCents: exactly two decimals + thousand separators', () => {
  assert.strictEqual(S.formatCents(0), '0.00');
  assert.strictEqual(S.formatCents(5), '0.05');
  assert.strictEqual(S.formatCents(30), '0.30');
  assert.strictEqual(S.formatCents(123456), '1,234.56');
  assert.strictEqual(S.formatCents(1000000), '10,000.00');
  assert.strictEqual(S.formatCents(999999999), '9,999,999.99');
  assert.strictEqual(S.formatCents(-12345), '-123.45');
});

test('sumEntries sums integer cents only', () => {
  const entries = [
    { type: 'income', amountCents: 10 },
    { type: 'income', amountCents: 20 },
    { type: 'expense', amountCents: 7 },
    { type: 'expense', amountCents: 3 }
  ];
  assert.strictEqual(S.sumEntries(entries, 'income'), 30);
  assert.strictEqual(S.sumEntries(entries, 'expense'), 10);
  assert.strictEqual(S.sumEntries(entries), 40);
});

/* ------------------------------ dates ------------------------------ */

section('Dates');

test('validateDate accepts real ISO dates only', () => {
  assert.strictEqual(S.validateDate('2026-08-13'), true);
  assert.strictEqual(S.validateDate('2024-02-29'), true);
  assert.strictEqual(S.validateDate('2026-02-30'), false);
  assert.strictEqual(S.validateDate('2026-13-01'), false);
  assert.strictEqual(S.validateDate('2026-00-10'), false);
  assert.strictEqual(S.validateDate('2026-8-3'), false);
  assert.strictEqual(S.validateDate('08/13/2026'), false);
  assert.strictEqual(S.validateDate(''), false);
  assert.strictEqual(S.validateDate('garbage'), false);
});

test('displayDate renders "Aug 13", never raw ISO', () => {
  const now = new Date();
  const y = now.getFullYear();
  const pad = (n) => String(n).padStart(2, '0');
  const iso = y + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate());
  const shown = S.displayDate(iso);
  assert.ok(shown.length >= 5, 'got: ' + shown);
  assert.ok(!/^\d{4}-\d{2}-\d{2}$/.test(shown), 'must not be raw ISO: ' + shown);
  assert.ok(shown.indexOf('Aug 13') === 0, 'should start with "Aug 13", got: ' + shown);
  if (y === 2026) assert.strictEqual(shown, 'Aug 13');
});

test('monthLabel and monthOf', () => {
  assert.strictEqual(S.monthLabel('2026-08'), 'Aug 2026');
  assert.strictEqual(S.monthOf('2026-08-13'), '2026-08');
  assert.strictEqual(S.monthOf(''), '');
});

test('todayISO is a valid ISO date', () => {
  assert.strictEqual(S.validateDate(S.todayISO()), true);
  assert.strictEqual(S.todayISO().length, 10);
});

/* --------------------------- store basics --------------------------- */

section('Store basics');

test('createStore works with no storage (memory-only)', () => {
  const st = S.createStore(null);
  assert.ok(Array.isArray(st.getState().entries));
  assert.ok(Array.isArray(st.getState().categories));
});

test('preset categories ship with the app, including uncategorized', () => {
  const st = S.createStore(null);
  const cats = st.getState().categories;
  assert.ok(cats.length >= 5);
  assert.ok(cats.some((c) => c.id === S.UNCATEGORIZED_ID));
  assert.ok(cats.some((c) => c.id === 'cat-food'));
});

test('addEntry stores a valid entry with an id', () => {
  const st = S.createStore(null);
  const r = st.addEntry({ type: 'expense', title: 'Coffee', amountCents: 450, date: '2026-08-13', categoryId: firstCategoryId(st) });
  assert.strictEqual(r.ok, true, r.error);
  assert.ok(r.entry.id);
  assert.strictEqual(st.getState().entries.length, 1);
});

test('empty title is rejected and nothing is stored', () => {
  const st = S.createStore(null);
  const cat = firstCategoryId(st);
  assert.strictEqual(st.addEntry({ type: 'expense', title: '', amountCents: 100, date: '2026-08-13', categoryId: cat }).ok, false);
  assert.strictEqual(st.addEntry({ type: 'expense', title: '   ', amountCents: 100, date: '2026-08-13', categoryId: cat }).ok, false);
  assert.strictEqual(st.getState().entries.length, 0);
});

test('invalid amounts and dates are rejected on add', () => {
  const st = S.createStore(null);
  const cat = firstCategoryId(st);
  assert.strictEqual(st.addEntry({ type: 'expense', title: 'X', amountCents: 0, date: '2026-08-13', categoryId: cat }).ok, false);
  assert.strictEqual(st.addEntry({ type: 'expense', title: 'X', amountCents: 100, date: '2026-02-30', categoryId: cat }).ok, false);
  assert.strictEqual(st.addEntry({ type: 'expense', title: 'X', amountCents: 100, date: '2026-08-13', categoryId: 'nope' }).ok, false);
  assert.strictEqual(st.getState().entries.length, 0);
});

test('updateEntry edits fields in place', () => {
  const st = S.createStore(null);
  const e = st.addEntry({ type: 'expense', title: 'Coffee', amountCents: 450, date: '2026-08-13', categoryId: firstCategoryId(st) }).entry;
  const r = st.updateEntry(e.id, { type: 'income', title: 'Refund', amountCents: 500, date: '2026-08-14', categoryId: e.categoryId });
  assert.strictEqual(r.ok, true, r.error);
  const got = st.getState().entries[0];
  assert.strictEqual(got.title, 'Refund');
  assert.strictEqual(got.type, 'income');
  assert.strictEqual(got.amountCents, 500);
  assert.strictEqual(got.id, e.id);
});

test('store exposes sortedEntries (used by the UI at boot)', () => {
  const st = S.createStore(null);
  const catId = S.UNCATEGORIZED_ID;
  st.addEntry({ type: 'expense', title: 'A', amountCents: 100, date: '2026-08-01', categoryId: catId });
  st.addEntry({ type: 'expense', title: 'B', amountCents: 100, date: '2026-08-13', categoryId: catId });
  const out = st.sortedEntries();
  assert.strictEqual(out.length, 2);
  assert.strictEqual(out[0].title, 'B');
  assert.strictEqual(out[1].title, 'A');
});

test('deleteEntry removes the entry', () => {
  const st = S.createStore(null);
  const e = st.addEntry({ type: 'expense', title: 'Coffee', amountCents: 450, date: '2026-08-13', categoryId: firstCategoryId(st) }).entry;
  assert.strictEqual(st.deleteEntry(e.id).ok, true);
  assert.strictEqual(st.getState().entries.length, 0);
  assert.strictEqual(st.deleteEntry(e.id).ok, false);
});

/* ---------------------------- categories ---------------------------- */

section('Categories');

test('addCategory assigns an id and keeps the name', () => {
  const st = S.createStore(null);
  const r = st.addCategory('Coffee');
  assert.strictEqual(r.ok, true, r.error);
  assert.ok(r.category.id);
  assert.strictEqual(r.category.name, 'Coffee');
});

test('empty category name is rejected', () => {
  const st = S.createStore(null);
  assert.strictEqual(st.addCategory('   ').ok, false);
  assert.strictEqual(st.renameCategory(firstCategoryId(st), '  ').ok, false);
});

test('renameCategory keeps the id; existing entries follow', () => {
  const st = S.createStore(null);
  const cat = st.addCategory('Food').category;
  const e = st.addEntry({ type: 'expense', title: 'Lunch', amountCents: 500, date: '2026-08-13', categoryId: cat.id }).entry;
  const r = st.renameCategory(cat.id, 'Groceries');
  assert.strictEqual(r.ok, true, r.error);
  assert.strictEqual(r.category.id, cat.id);
  assert.strictEqual(st.getState().entries[0].categoryId, cat.id);
  assert.strictEqual(st.categoryName(cat.id), 'Groceries');
  assert.strictEqual(st.categoryName(e.categoryId), 'Groceries');
});

test('deleteCategory moves its entries to uncategorized', () => {
  const st = S.createStore(null);
  const cat = st.addCategory('Food').category;
  st.addEntry({ type: 'expense', title: 'Lunch', amountCents: 500, date: '2026-08-13', categoryId: cat.id });
  const r = st.deleteCategory(cat.id);
  assert.strictEqual(r.ok, true, r.error);
  assert.strictEqual(st.getState().entries[0].categoryId, S.UNCATEGORIZED_ID);
  assert.strictEqual(st.getState().categories.some((c) => c.id === cat.id), false);
});

test('uncategorized is built-in and cannot be deleted', () => {
  const st = S.createStore(null);
  assert.strictEqual(st.deleteCategory(S.UNCATEGORIZED_ID).ok, false);
  assert.strictEqual(st.getState().categories.some((c) => c.id === S.UNCATEGORIZED_ID), true);
});

test('deleting a category removes its budget', () => {
  const st = S.createStore(null);
  const cat = st.addCategory('Food').category;
  st.setBudget(cat.id, 10000);
  st.deleteCategory(cat.id);
  assert.strictEqual(st.getState().budgets[cat.id], undefined);
});

/* ------------------------ filters and search ------------------------ */

section('Filters and search');

test('month filter keeps only that month', () => {
  const entries = [
    makeEntry({ id: 'a', date: '2026-08-01' }),
    makeEntry({ id: 'b', date: '2026-07-31' })
  ];
  const out = S.filterEntries(entries, { month: '2026-08' });
  assert.strictEqual(out.length, 1);
  assert.strictEqual(out[0].id, 'a');
});

test('type filter', () => {
  const entries = [makeEntry({ id: 'a', type: 'income' }), makeEntry({ id: 'b', type: 'expense' })];
  assert.strictEqual(S.filterEntries(entries, { type: 'income' }).length, 1);
  assert.strictEqual(S.filterEntries(entries, { type: 'expense' }).length, 1);
  assert.strictEqual(S.filterEntries(entries, { type: 'all' }).length, 2);
});

test('search matches titles case-insensitively', () => {
  const entries = [makeEntry({ id: 'a', title: 'Morning Coffee' }), makeEntry({ id: 'b', title: 'Rent' })];
  assert.strictEqual(S.filterEntries(entries, { query: 'coffee' }).length, 1);
  assert.strictEqual(S.filterEntries(entries, { query: 'COFFEE' }).length, 1);
  assert.strictEqual(S.filterEntries(entries, { query: 'oFf' }).length, 1);
  assert.strictEqual(S.filterEntries(entries, { query: '   ' }).length, 2);
  assert.strictEqual(S.filterEntries(entries, { query: 'zzz' }).length, 0);
});

test('search matches titles only, not category names', () => {
  const entries = [makeEntry({ id: 'a', title: 'Rent', categoryId: 'cat-food' })];
  assert.strictEqual(S.filterEntries(entries, { query: 'food' }).length, 0);
});

test('filters combine', () => {
  const entries = [
    makeEntry({ id: 'a', type: 'expense', title: 'Coffee', date: '2026-08-01', categoryId: 'cat-food' }),
    makeEntry({ id: 'b', type: 'income', title: 'Coffee refund', date: '2026-08-02', categoryId: 'cat-food' }),
    makeEntry({ id: 'c', type: 'expense', title: 'Coffee', date: '2026-07-01', categoryId: 'cat-food' })
  ];
  const out = S.filterEntries(entries, { month: '2026-08', type: 'expense', query: 'coffee' });
  assert.strictEqual(out.length, 1);
  assert.strictEqual(out[0].id, 'a');
});

test('sortEntries is newest first (date desc, id desc on ties)', () => {
  const entries = [
    makeEntry({ id: 'old', date: '2026-08-01' }),
    makeEntry({ id: 'new', date: '2026-08-13' }),
    makeEntry({ id: 'tie-a', date: '2026-08-05' }),
    makeEntry({ id: 'tie-b', date: '2026-08-05' })
  ];
  const out = S.sortEntries(entries);
  assert.deepStrictEqual(out.map((e) => e.id), ['new', 'tie-b', 'tie-a', 'old']);
});

test('months() lists present months newest first', () => {
  const st = S.createStore(null);
  const cat = firstCategoryId(st);
  st.addEntry({ type: 'expense', title: 'A', amountCents: 100, date: '2026-08-01', categoryId: cat });
  st.addEntry({ type: 'expense', title: 'B', amountCents: 100, date: '2026-06-15', categoryId: cat });
  assert.deepStrictEqual(st.months(), ['2026-08', '2026-06']);
});

/* ----------------------------- debounce ----------------------------- */

section('Debounce (pure, testable)');

test('debounce fires only after the silence window', () => {
  const t = fakeTimers();
  const d = S.createDebouncer(200, t);
  let calls = 0;
  d.call(() => { calls += 1; });
  t.advance(100);
  assert.strictEqual(calls, 0);
  t.advance(99);
  assert.strictEqual(calls, 0);
  t.advance(1);
  assert.strictEqual(calls, 1);
});

test('rapid calls collapse into a single fire', () => {
  const t = fakeTimers();
  const d = S.createDebouncer(200, t);
  let calls = 0;
  for (let i = 0; i < 5; i += 1) {
    d.call(() => { calls += 1; });
    t.advance(50);
  }
  assert.strictEqual(calls, 0);
  t.advance(200);
  assert.strictEqual(calls, 1);
});

test('debounce fires the latest call only', () => {
  const t = fakeTimers();
  const d = S.createDebouncer(200, t);
  let value = 0;
  d.call(() => { value = 1; });
  d.call(() => { value = 2; });
  t.advance(200);
  assert.strictEqual(value, 2);
});

test('flush fires immediately; cancel drops the pending call', () => {
  const t = fakeTimers();
  const d = S.createDebouncer(200, t);
  let calls = 0;
  d.call(() => { calls += 1; });
  d.flush();
  assert.strictEqual(calls, 1);
  d.call(() => { calls += 1; });
  d.cancel();
  t.advance(200);
  assert.strictEqual(calls, 1);
});

/* ----------------------------- budgets ------------------------------ */

section('Budgets');

test('progress clamps at 100% and flags overspend', () => {
  assert.deepStrictEqual(S.budgetProgress(5000, 10000), { ratio: 0.5, overspent: false });
  assert.deepStrictEqual(S.budgetProgress(0, 10000), { ratio: 0, overspent: false });
  const over = S.budgetProgress(15000, 10000);
  assert.strictEqual(over.ratio, 1);
  assert.strictEqual(over.overspent, true);
  assert.ok(over.ratio <= 1);
  const exact = S.budgetProgress(10000, 10000);
  assert.strictEqual(exact.ratio, 1);
  assert.strictEqual(exact.overspent, false);
  assert.deepStrictEqual(S.budgetProgress(100, 0), { ratio: 0, overspent: false });
});

test('setBudget stores integer cents and rejects junk', () => {
  const st = S.createStore(null);
  const catId = firstCategoryId(st);
  assert.strictEqual(st.setBudget(catId, 10000).ok, true);
  assert.strictEqual(st.getState().budgets[catId], 10000);
  assert.strictEqual(st.setBudget(catId, 0).ok, true);
  assert.strictEqual(st.getState().budgets[catId], undefined);
  assert.strictEqual(st.setBudget(catId, -5).ok, false);
});

test('monthly spending against a budget', () => {
  const st = S.createStore(null);
  const catId = firstCategoryId(st);
  st.addEntry({ type: 'expense', title: 'A', amountCents: 4000, date: '2026-08-01', categoryId: catId });
  st.addEntry({ type: 'expense', title: 'B', amountCents: 2000, date: '2026-07-01', categoryId: catId });
  st.setBudget(catId, 10000);
  const spent = S.sumEntries(S.filterEntries(st.getState().entries, { month: '2026-08', categoryId: catId, type: 'expense' }), 'expense');
  assert.strictEqual(spent, 4000);
  const prog = S.budgetProgress(spent, st.getState().budgets[catId]);
  assert.strictEqual(prog.ratio, 0.4);
  assert.strictEqual(prog.overspent, false);
});

/* ------------------------- dashboard helpers ------------------------ */

section('Dashboard helpers');

test('totalsForMonth: income, expenses, and balance sign', () => {
  const st = S.createStore(null);
  const catId = firstCategoryId(st);
  st.addEntry({ type: 'income', title: 'Salary', amountCents: 500000, date: '2026-08-01', categoryId: catId });
  st.addEntry({ type: 'expense', title: 'Rent', amountCents: 200000, date: '2026-08-02', categoryId: catId });
  const t = st.totalsForMonth('2026-08');
  assert.strictEqual(t.income, 500000);
  assert.strictEqual(t.expenses, 200000);
  assert.strictEqual(t.income - t.expenses, 300000);
  assert.strictEqual(S.formatCents(t.income - t.expenses), '3,000.00');
});

test('balance is negative when expenses exceed income', () => {
  const st = S.createStore(null);
  const catId = firstCategoryId(st);
  st.addEntry({ type: 'income', title: 'In', amountCents: 10000, date: '2026-08-01', categoryId: catId });
  st.addEntry({ type: 'expense', title: 'Out', amountCents: 15000, date: '2026-08-02', categoryId: catId });
  const t = st.totalsForMonth('2026-08');
  assert.strictEqual(t.income - t.expenses, -5000);
  assert.strictEqual(S.formatCents(t.income - t.expenses), '-50.00');
});

test('lastSixMonths returns six ordered month buckets', () => {
  const st = S.createStore(null);
  const m = st.lastSixMonths();
  assert.strictEqual(m.length, 6);
  for (const x of m) assert.ok(/^\d{4}-\d{2}$/.test(x.month));
  for (let i = 1; i < m.length; i += 1) assert.ok(m[i - 1].month < m[i].month);
  const now = new Date();
  const last = m[m.length - 1].month;
  assert.strictEqual(last, now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0'));
});

/* ------------------------------- CSV -------------------------------- */

section('CSV import/export');

test('export has a header row', () => {
  const st = S.createStore(null);
  const csv = st.exportCSV();
  assert.ok(csv.startsWith('id,type,title,amount,date,categoryId,categoryName'));
});

test('export escapes commas, quotes, and newlines (RFC-4180)', () => {
  const st = S.createStore(null);
  const cat = firstCategoryId(st);
  const title = 'Coffee, "dark" & tea\nnext line\r\nlast';
  st.addEntry({ type: 'expense', title: title, amountCents: 450, date: '2026-08-13', categoryId: cat });
  const csv = st.exportCSV();
  const rows = S.csvParse(csv);
  assert.strictEqual(rows.length, 2);
  assert.strictEqual(rows[1][2], title);
  assert.strictEqual(rows[1][3], '4.50');
});

test('round-trip: export, then import into a fresh app — same data', () => {
  const a = S.createStore(null);
  const cat = a.addCategory('Side Hustle').category;
  a.addEntry({ type: 'income', title: 'Freelance, invoice #12', amountCents: 123456, date: '2026-08-13', categoryId: cat.id });
  a.addEntry({ type: 'expense', title: 'Rent', amountCents: 150000, date: '2026-08-01', categoryId: S.UNCATEGORIZED_ID });
  a.addEntry({ type: 'expense', title: 'Coffee "latte"', amountCents: 450, date: '2026-07-28', categoryId: cat.id });

  const csv = a.exportCSV();
  const b = S.createStore(null);
  const res = b.importCSV(csv);
  assert.strictEqual(res.ok, true, res.error);
  assert.strictEqual(res.imported, 3);
  assert.strictEqual(res.skipped, 0);
  assert.deepStrictEqual(b.getState().entries, a.getState().entries);

  const catB = b.getState().categories.find((c) => c.id === cat.id);
  assert.ok(catB, 'custom category should be recreated');
  assert.strictEqual(catB.name, 'Side Hustle');
});

test('import skips malformed rows and never touches existing data', () => {
  const st = S.createStore(null);
  const cat = firstCategoryId(st);
  st.addEntry({ type: 'expense', title: 'Keep me', amountCents: 100, date: '2026-08-01', categoryId: cat });
  const csv = [
    'id,type,title,amount,date,categoryId,categoryName',
    'n1,expense,Good row,12.34,2026-08-02,' + cat + ',',
    'n6,income,Another good,7.50,2026-08-03,' + cat + ',',
    'n2,expense,Bad amount,abc,2026-08-02,' + cat + ',',
    'n3,income,,100.00,2026-08-02,' + cat + ',',
    'n4,expense,Good too,5.00,2026-13-99,' + cat + ',',
    'n5,expense,Short row,5.00,2026-08-02',
    ''
  ].join('\n');
  const res = st.importCSV(csv);
  assert.strictEqual(res.ok, true, res.error);
  assert.strictEqual(res.imported, 2);
  assert.strictEqual(res.skipped, 4);
  assert.strictEqual(st.getState().entries.length, 3);
  assert.ok(st.getState().entries.some((e) => e.title === 'Keep me'));
  assert.ok(st.getState().entries.some((e) => e.title === 'Good row'));
});

test('a broken file aborts and leaves data untouched', () => {
  const st = S.createStore(null);
  const cat = firstCategoryId(st);
  st.addEntry({ type: 'expense', title: 'Keep me', amountCents: 100, date: '2026-08-01', categoryId: cat });
  const before = JSON.stringify(st.getState());
  const res = st.importCSV('this is not csv at all');
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.imported, 0);
  assert.strictEqual(JSON.stringify(st.getState()), before);
});

test('import of empty input fails safely', () => {
  const st = S.createStore(null);
  assert.strictEqual(st.importCSV('').ok, false);
  assert.strictEqual(st.importCSV('\n\n').ok, false);
  assert.strictEqual(st.getState().entries.length, 0);
});

test('import never creates duplicate ids', () => {
  const st = S.createStore(null);
  const csv = 'id,type,title,amount,date,categoryId,categoryName\n' +
    'dup,expense,First,1.00,2026-08-01,uncategorized,\n' +
    'dup,expense,Second,2.00,2026-08-02,uncategorized,\n';
  const res = st.importCSV(csv);
  assert.strictEqual(res.imported, 1);
  assert.strictEqual(res.skipped, 1);
  assert.strictEqual(st.getState().entries.length, 1);
});

/* ------------------------ corrupt storage --------------------------- */

section('Corrupt storage recovery');

test('garbage JSON recovers to a clean state and heals storage', () => {
  const fs = fakeStorage('{not json!!!');
  const st = S.createStore(fs);
  const state = st.getState();
  assert.ok(Array.isArray(state.entries));
  assert.ok(state.categories.length > 0);
  const healed = JSON.parse(fs.getItem(S.STORAGE_KEY));
  assert.ok(Array.isArray(healed.entries));
});

test('wrong-shape JSON recovers to a clean state', () => {
  const fs = fakeStorage(JSON.stringify({ entries: 'nope', categories: 42 }));
  const st = S.createStore(fs);
  assert.ok(Array.isArray(st.getState().entries));
  assert.ok(st.getState().categories.length > 0);
});

test('malformed entries are dropped, valid entries kept', () => {
  const good = { id: 'g1', type: 'expense', title: 'Rent', amountCents: 1000, date: '2026-08-01', categoryId: 'cat-food' };
  const bad = { id: 'b1', type: 'expense', title: 'X', amountCents: 'NaN', date: '2026-08-01', categoryId: 'cat-food' };
  const fs = fakeStorage(JSON.stringify({
    categories: [{ id: 'cat-food', name: 'Food' }, { id: 'uncategorized', name: 'Uncategorized' }],
    entries: [good, bad],
    budgets: {}
  }));
  const st = S.createStore(fs);
  assert.strictEqual(st.getState().entries.length, 1);
  assert.strictEqual(st.getState().entries[0].id, 'g1');
});

test('stored entries with unknown categories map to uncategorized', () => {
  const fs = fakeStorage(JSON.stringify({
    categories: [{ id: 'cat-food', name: 'Food' }, { id: 'uncategorized', name: 'Uncategorized' }],
    entries: [{ id: 'x', type: 'expense', title: 'T', amountCents: 100, date: '2026-08-01', categoryId: 'does-not-exist' }],
    budgets: {}
  }));
  const st = S.createStore(fs);
  assert.strictEqual(st.getState().entries[0].categoryId, 'uncategorized');
});

test('mutations persist to storage and reload', () => {
  const fs = fakeStorage();
  const st = S.createStore(fs);
  st.addEntry({ type: 'expense', title: 'Persisted', amountCents: 250, date: '2026-08-13', categoryId: S.UNCATEGORIZED_ID });
  const st2 = S.createStore(fs);
  assert.strictEqual(st2.getState().entries.length, 1);
  assert.strictEqual(st2.getState().entries[0].title, 'Persisted');
});

/* ------------------------------ summary ----------------------------- */

console.log('\n----------------------------------------');
console.log(passed + ' passed, ' + failed + ' failed');
if (failed > 0) {
  console.log('\nFailures:');
  for (const f of failures) console.log('  - ' + f);
  process.exitCode = 1;
}
