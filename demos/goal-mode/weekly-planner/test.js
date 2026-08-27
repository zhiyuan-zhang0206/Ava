'use strict';

/**
 * test.js — a pure Node test script (no npm dependencies):
 *   node test.js
 * Imports the real data-layer functions from store.js and the pure helpers
 * from app.js; covers: add (trim / 120-char truncation / empty-title
 * rejection), no dedup, newest-first within a day, cross-day moves, delete,
 * localStorage round-trip and corruption recovery, and the 20-per-column
 * window. Prints a clear summary; exits non-zero on any failure.
 */

const assert = require('assert');
const Store = require('./store.js');
const App = require('./app.js');

let passed = 0;
let failed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('  ok   ' + name);
  } catch (err) {
    failed += 1;
    failures.push({ name: name, err: err });
    console.log('  FAIL ' + name + ' - ' + err.message);
  }
}

/** In-memory localStorage for round-trip testing. */
function fakeStorage(initial) {
  const m = new Map(Object.entries(initial || {}));
  return {
    getItem(k) { return m.has(k) ? m.get(k) : null; },
    setItem(k, v) { m.set(k, String(v)); },
    removeItem(k) { m.delete(k); },
    _map: m
  };
}

console.log('weekly-planner tests\n');

// ---------------------------------------------------------------- store.js

test('module exposes the full API', () => {
  for (const fn of [
    'addTask', 'moveTask', 'deleteTask', 'getTasks', 'normalizeTitle',
    'createState', 'serialize', 'parseStored', 'saveState', 'loadState',
    'windowTasks', 'clampDay', 'taskCount'
  ]) {
    assert.strictEqual(typeof Store[fn], 'function', 'store.' + fn);
  }
  assert.strictEqual(typeof App.formatDayHeader, 'function');
  assert.strictEqual(typeof App.initApp, 'function');
});

test('addTask trims title whitespace', () => {
  const s = Store.createState();
  const r = Store.addTask(s, 0, '  buy groceries  ');
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.task.title, 'buy groceries');
  assert.strictEqual(Store.getTasks(s, 0).length, 1);
});

test('addTask truncates past 120 chars in the stored data (not just visually)', () => {
  const s = Store.createState();
  const long = 'x'.repeat(200);
  const r = Store.addTask(s, 0, '  ' + long + '  ');
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.task.title.length, 120);
  assert.strictEqual(r.task.title, 'x'.repeat(120));
  // After a persist/read round-trip, the stored title is still truncated
  const reloaded = Store.parseStored(Store.serialize(s));
  assert.strictEqual(reloaded.days['0'][0].title.length, 120);
});

test('addTask rejects empty / whitespace-only titles and leaves state unchanged', () => {
  const s = Store.createState();
  assert.strictEqual(Store.addTask(s, 0, '').ok, false);
  assert.strictEqual(Store.addTask(s, 0, '   ').ok, false);
  assert.strictEqual(Store.addTask(s, 0, '\t\n').ok, false);
  assert.strictEqual(Store.addTask(s, 0, null).ok, false);
  assert.strictEqual(Store.addTask(s, 0, undefined).ok, false);
  assert.strictEqual(Store.getTasks(s, 0).length, 0);
});

test('identical titles are never merged/deduped (same day or different days)', () => {
  const s = Store.createState();
  Store.addTask(s, 0, 'write weekly report');
  Store.addTask(s, 0, 'write weekly report');
  Store.addTask(s, 3, 'write weekly report');
  assert.strictEqual(Store.getTasks(s, 0).length, 2);
  assert.strictEqual(Store.getTasks(s, 3).length, 1);
  const day0 = Store.getTasks(s, 0);
  assert.notStrictEqual(day0[0].id, day0[1].id);
});

test('the newest task added on a day sorts first', () => {
  const s = Store.createState();
  Store.addTask(s, 2, 'first');
  Store.addTask(s, 2, 'second');
  Store.addTask(s, 2, 'third');
  assert.deepStrictEqual(
    Store.getTasks(s, 2).map((t) => t.title),
    ['third', 'second', 'first']
  );
});

test('moveTask moves across days: first in the target day, removed from the source', () => {
  const s = Store.createState();
  Store.addTask(s, 0, 'moved task');
  Store.addTask(s, 0, 'stays on Monday');
  const moved = Store.getTasks(s, 0).find((t) => t.title === 'moved task');
  const r = Store.moveTask(s, moved.id, 4);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.task.day, 4);
  assert.strictEqual(Store.getTasks(s, 0).some((t) => t.id === moved.id), false);
  const target = Store.getTasks(s, 4);
  assert.strictEqual(target.length, 1);
  assert.strictEqual(target[0].id, moved.id);
});

test('moveTask to the same day is a harmless no-op', () => {
  const s = Store.createState();
  const r0 = Store.addTask(s, 1, 'stays put');
  const r = Store.moveTask(s, r0.task.id, 1);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.moved, false);
  assert.strictEqual(Store.getTasks(s, 1).length, 1);
});

test('moveTask rejects missing tasks and out-of-range days', () => {
  const s = Store.createState();
  assert.strictEqual(Store.moveTask(s, 9999, 2).ok, false);
  assert.strictEqual(Store.moveTask(s, 1, 7).ok, false);
  assert.strictEqual(Store.moveTask(s, 1, -1).ok, false);
});

test('deleteTask deletes a task; unknown ids return an error', () => {
  const s = Store.createState();
  const r0 = Store.addTask(s, 5, 'to delete');
  Store.addTask(s, 5, 'keep');
  const r = Store.deleteTask(s, r0.task.id);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(Store.getTasks(s, 5).length, 1);
  assert.strictEqual(Store.getTasks(s, 5)[0].title, 'keep');
  assert.strictEqual(Store.deleteTask(s, 424242).ok, false);
});

test('localStorage round-trip preserves tasks, order, and nextId', () => {
  const storage = fakeStorage();
  const s = Store.createState();
  Store.addTask(s, 0, 'persisted task A');
  Store.addTask(s, 0, 'persisted task B');
  Store.addTask(s, 6, 'Sunday task');
  assert.strictEqual(Store.saveState(storage, s), true);
  const loaded = Store.loadState(storage);
  assert.deepStrictEqual(
    Store.getTasks(loaded, 0).map((t) => t.title),
    ['persisted task B', 'persisted task A']
  );
  assert.deepStrictEqual(
    Store.getTasks(loaded, 6).map((t) => t.title),
    ['Sunday task']
  );
  // nextId keeps increasing; new task ids never collide with existing ones
  const r = Store.addTask(loaded, 0, 'new task');
  assert.strictEqual(r.task.id, s.nextId);
  assert.strictEqual(
    Store.getTasks(loaded, 0).filter((t) => t.id === r.task.id).length,
    1
  );
});

test('corrupted-data recovery: any garbage falls back to a clean empty state, never throws', () => {
  const bad = [
    '{oops',
    'null',
    '[]',
    '"just a string"',
    '42',
    '{"nextId":"x","days":[]}',
    '{"nextId":3,"days":{"0":[{"id":"a"}]}}',
    undefined
  ];
  for (const raw of bad) {
    const s = Store.parseStored(raw);
    assert.strictEqual(s.nextId >= 1, true);
    assert.deepStrictEqual(s.days, {});
  }
  // loadState is equally safe against corrupted storage
  const storage = fakeStorage({ [Store.STORAGE_KEY]: '%%%corrupt%%%' });
  const loaded = Store.loadState(storage);
  assert.deepStrictEqual(loaded.days, {});
});

test('partially corrupted data: invalid entries dropped, valid tasks kept', () => {
  const mixed = JSON.stringify({
    nextId: 2,
    days: {
      '0': [
        { id: 1, title: '  valid  ', day: 0, createdAt: 1 },
        { id: 'bad', title: 'invalid' },
        null,
        { id: 3, title: '   ', day: 0 },
        { id: 4, title: 'x'.repeat(200), day: 0, createdAt: 2 }
      ],
      '2': 'not-an-array'
    }
  });
  const s = Store.parseStored(mixed);
  assert.strictEqual(s.days['0'].length, 2);
  assert.strictEqual(s.days['0'][0].title, 'valid');
  assert.strictEqual(s.days['0'][1].title.length, 120);
  assert.strictEqual(s.days['2'], undefined);
  // The largest valid task id is 4; nextId must advance to 5 to avoid collisions
  assert.strictEqual(s.nextId, 5);
});

test('recovery advances nextId past the largest surviving id to prevent collisions', () => {
  const s = Store.parseStored(
    JSON.stringify({ nextId: 1, days: { '0': [{ id: 5, title: 'x', day: 0, createdAt: 1 }] } })
  );
  assert.strictEqual(s.nextId, 6);
});

test('save/load degrade gracefully when storage is unavailable (no throw)', () => {
  const broken = {
    getItem() { throw new Error('blocked'); },
    setItem() { throw new Error('blocked'); }
  };
  const s = Store.createState();
  Store.addTask(s, 0, 'in-memory task');
  assert.strictEqual(Store.saveState(broken, s), false);
  const loaded = Store.loadState(broken);
  assert.deepStrictEqual(loaded.days, {});
});

test('windowTasks: at most 20 per column plus the hasMore flag', () => {
  const s = Store.createState();
  for (let i = 0; i < 25; i++) Store.addTask(s, 0, 'task' + i);
  const win = Store.windowTasks(Store.getTasks(s, 0), Store.DAY_WINDOW);
  assert.strictEqual(win.visible.length, 20);
  assert.strictEqual(win.hasMore, true);
  assert.strictEqual(win.total, 25);
  // The window holds the day's newest 20 tasks
  assert.strictEqual(win.visible[0].title, 'task24');
  assert.strictEqual(win.visible[19].title, 'task5');
  // The default limit is 20
  const winDefault = Store.windowTasks(Store.getTasks(s, 0));
  assert.strictEqual(winDefault.visible.length, 20);

  const s2 = Store.createState();
  for (let i = 0; i < 20; i++) Store.addTask(s2, 1, 'x' + i);
  const w2 = Store.windowTasks(Store.getTasks(s2, 1), Store.DAY_WINDOW);
  assert.strictEqual(w2.visible.length, 20);
  assert.strictEqual(w2.hasMore, false);

  const w3 = Store.windowTasks([], Store.DAY_WINDOW);
  assert.strictEqual(w3.visible.length, 0);
  assert.strictEqual(w3.hasMore, false);
});

// ---------------------------------------------------------------- app.js pure helpers

test('formatDayHeader renders Mon 8/13 form with no ISO fragments', () => {
  assert.strictEqual(App.formatDayHeader(new Date(2026, 7, 13)), 'Thu 8/13');
  assert.strictEqual(App.formatDayHeader(new Date(2026, 7, 10)), 'Mon 8/10');
  assert.strictEqual(App.formatDayHeader(new Date(2026, 7, 16)), 'Sun 8/16');
  assert.strictEqual(App.formatDayHeader(new Date(2026, 0, 5)), 'Mon 1/5');
  assert.strictEqual(App.formatDayHeader(new Date(2026, 11, 25)), 'Fri 12/25');
  // No ISO fingerprints allowed (hyphens, zero-padding, T, colons)
  assert.strictEqual(/[-T:]/.test(App.formatDayHeader(new Date(2026, 11, 25))), false);
});

test('toMonday returns the current week\u2019s Monday (week starts Monday)', () => {
  const mon = App.toMonday(new Date(2026, 7, 13)); // Thursday
  assert.strictEqual(mon.getFullYear(), 2026);
  assert.strictEqual(mon.getMonth(), 7);
  assert.strictEqual(mon.getDate(), 10);
  const sun = App.toMonday(new Date(2026, 7, 16)); // Sunday -> this week's Monday
  assert.strictEqual(sun.getDate(), 10);
  const itself = App.toMonday(new Date(2026, 7, 10)); // Monday
  assert.strictEqual(itself.getDate(), 10);
  const jan1 = App.toMonday(new Date(2026, 0, 1)); // Thursday -> 2025-12-29
  assert.strictEqual(jan1.getFullYear(), 2025);
  assert.strictEqual(jan1.getMonth(), 11);
  assert.strictEqual(jan1.getDate(), 29);
});

test('dayDates returns 7 consecutive days from Monday', () => {
  const ds = App.dayDates(new Date(2026, 7, 13));
  assert.strictEqual(ds.length, 7);
  assert.strictEqual(App.formatDayHeader(ds[0]), 'Mon 8/10');
  assert.strictEqual(App.formatDayHeader(ds[6]), 'Sun 8/16');
});

test('adjacentDay never crosses the week boundary', () => {
  assert.strictEqual(App.adjacentDay(2, 1), 3);
  assert.strictEqual(App.adjacentDay(2, -1), 1);
  assert.strictEqual(App.adjacentDay(0, -1), null);
  assert.strictEqual(App.adjacentDay(6, 1), null);
});

// ---------------------------------------------------------------- summary

console.log('');
console.log(passed + ' passed, ' + failed + ' failed');
if (failed > 0) {
  console.log('Failed cases:');
  for (const f of failures) console.log('  - ' + f.name + ': ' + f.err.message);
  process.exitCode = 1;
}
