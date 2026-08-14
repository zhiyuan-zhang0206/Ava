'use strict';

/**
 * test.js — 纯 Node 测试脚本（无任何 npm 依赖）：
 *   node test.js
 * 从 store.js 导入真实数据层函数，从 app.js 导入纯工具函数，
 * 覆盖：新增（trim / 120 截断 / 空标题拒绝）、不去重、同日最新在前、
 * 跨天移动、删除、localStorage 持久化往返与损坏恢复、每列 20 条窗口。
 * 打印清晰汇总，任一失败以非零码退出。
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

/** 内存版 localStorage，便于测试往返。 */
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

test('模块暴露完整 API', () => {
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

test('addTask 去除标题首尾空白', () => {
  const s = Store.createState();
  const r = Store.addTask(s, 0, '  买菜  ');
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.task.title, '买菜');
  assert.strictEqual(Store.getTasks(s, 0).length, 1);
});

test('addTask 超过 120 字符在存储数据里截断（不只是视觉截断）', () => {
  const s = Store.createState();
  const long = '甲'.repeat(200);
  const r = Store.addTask(s, 0, '  ' + long + '  ');
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.task.title.length, 120);
  assert.strictEqual(r.task.title, '甲'.repeat(120));
  // 持久化后再读出来，存的也是截断后的标题
  const reloaded = Store.parseStored(Store.serialize(s));
  assert.strictEqual(reloaded.days['0'][0].title.length, 120);
});

test('addTask 拒绝空标题 / 纯空白标题，且状态不变', () => {
  const s = Store.createState();
  assert.strictEqual(Store.addTask(s, 0, '').ok, false);
  assert.strictEqual(Store.addTask(s, 0, '   ').ok, false);
  assert.strictEqual(Store.addTask(s, 0, '\t\n').ok, false);
  assert.strictEqual(Store.addTask(s, 0, null).ok, false);
  assert.strictEqual(Store.addTask(s, 0, undefined).ok, false);
  assert.strictEqual(Store.getTasks(s, 0).length, 0);
});

test('相同标题绝不合并/去重（同一天或不同天）', () => {
  const s = Store.createState();
  Store.addTask(s, 0, '写周报');
  Store.addTask(s, 0, '写周报');
  Store.addTask(s, 3, '写周报');
  assert.strictEqual(Store.getTasks(s, 0).length, 2);
  assert.strictEqual(Store.getTasks(s, 3).length, 1);
  const day0 = Store.getTasks(s, 0);
  assert.notStrictEqual(day0[0].id, day0[1].id);
});

test('同一天内最新添加的任务排在最前', () => {
  const s = Store.createState();
  Store.addTask(s, 2, '第一');
  Store.addTask(s, 2, '第二');
  Store.addTask(s, 2, '第三');
  assert.deepStrictEqual(
    Store.getTasks(s, 2).map((t) => t.title),
    ['第三', '第二', '第一']
  );
});

test('moveTask 跨天移动，目标天里排在最前，原天移除', () => {
  const s = Store.createState();
  Store.addTask(s, 0, '被移动的任务');
  Store.addTask(s, 0, '留在周一的');
  const moved = Store.getTasks(s, 0).find((t) => t.title === '被移动的任务');
  const r = Store.moveTask(s, moved.id, 4);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.task.day, 4);
  assert.strictEqual(Store.getTasks(s, 0).some((t) => t.id === moved.id), false);
  const target = Store.getTasks(s, 4);
  assert.strictEqual(target.length, 1);
  assert.strictEqual(target[0].id, moved.id);
});

test('moveTask 移到同一天是无害的空操作', () => {
  const s = Store.createState();
  const r0 = Store.addTask(s, 1, '原地不动');
  const r = Store.moveTask(s, r0.task.id, 1);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.moved, false);
  assert.strictEqual(Store.getTasks(s, 1).length, 1);
});

test('moveTask 拒绝不存在的任务与越界天', () => {
  const s = Store.createState();
  assert.strictEqual(Store.moveTask(s, 9999, 2).ok, false);
  assert.strictEqual(Store.moveTask(s, 1, 7).ok, false);
  assert.strictEqual(Store.moveTask(s, 1, -1).ok, false);
});

test('deleteTask 删除任务；未知 id 返回错误', () => {
  const s = Store.createState();
  const r0 = Store.addTask(s, 5, '要删的');
  Store.addTask(s, 5, '留下的');
  const r = Store.deleteTask(s, r0.task.id);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(Store.getTasks(s, 5).length, 1);
  assert.strictEqual(Store.getTasks(s, 5)[0].title, '留下的');
  assert.strictEqual(Store.deleteTask(s, 424242).ok, false);
});

test('localStorage 往返：任务、顺序、nextId 全部保留', () => {
  const storage = fakeStorage();
  const s = Store.createState();
  Store.addTask(s, 0, '持久化任务A');
  Store.addTask(s, 0, '持久化任务B');
  Store.addTask(s, 6, '周日任务');
  assert.strictEqual(Store.saveState(storage, s), true);
  const loaded = Store.loadState(storage);
  assert.deepStrictEqual(
    Store.getTasks(loaded, 0).map((t) => t.title),
    ['持久化任务B', '持久化任务A']
  );
  assert.deepStrictEqual(
    Store.getTasks(loaded, 6).map((t) => t.title),
    ['周日任务']
  );
  // nextId 继续递增，新任务 id 不与已有任务冲突
  const r = Store.addTask(loaded, 0, '新增任务');
  assert.strictEqual(r.task.id, s.nextId);
  assert.strictEqual(
    Store.getTasks(loaded, 0).filter((t) => t.id === r.task.id).length,
    1
  );
});

test('损坏数据恢复：任何垃圾都回退为干净空状态，绝不抛出', () => {
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
  // loadState 遇到损坏存储同样安全
  const storage = fakeStorage({ [Store.STORAGE_KEY]: '%%%corrupt%%%' });
  const loaded = Store.loadState(storage);
  assert.deepStrictEqual(loaded.days, {});
});

test('部分损坏数据：丢弃非法条目，保留合法任务', () => {
  const mixed = JSON.stringify({
    nextId: 2,
    days: {
      '0': [
        { id: 1, title: '  合法  ', day: 0, createdAt: 1 },
        { id: 'bad', title: '非法' },
        null,
        { id: 3, title: '   ', day: 0 },
        { id: 4, title: '超长'.repeat(100), day: 0, createdAt: 2 }
      ],
      '2': 'not-an-array'
    }
  });
  const s = Store.parseStored(mixed);
  assert.strictEqual(s.days['0'].length, 2);
  assert.strictEqual(s.days['0'][0].title, '合法');
  assert.strictEqual(s.days['0'][1].title.length, 120);
  assert.strictEqual(s.days['2'], undefined);
  // 合法任务最大 id 为 4，nextId 必须推进到 5 防止冲突
  assert.strictEqual(s.nextId, 5);
});

test('损坏数据恢复时 nextId 至少大于现存最大 id，防止冲突', () => {
  const s = Store.parseStored(
    JSON.stringify({ nextId: 1, days: { '0': [{ id: 5, title: 'x', day: 0, createdAt: 1 }] } })
  );
  assert.strictEqual(s.nextId, 6);
});

test('存储不可用时保存/读取优雅降级（不抛错）', () => {
  const broken = {
    getItem() { throw new Error('blocked'); },
    setItem() { throw new Error('blocked'); }
  };
  const s = Store.createState();
  Store.addTask(s, 0, '内存任务');
  assert.strictEqual(Store.saveState(broken, s), false);
  const loaded = Store.loadState(broken);
  assert.deepStrictEqual(loaded.days, {});
});

test('windowTasks：每列最多显示 20 条 + hasMore 标记', () => {
  const s = Store.createState();
  for (let i = 0; i < 25; i++) Store.addTask(s, 0, '任务' + i);
  const win = Store.windowTasks(Store.getTasks(s, 0), Store.DAY_WINDOW);
  assert.strictEqual(win.visible.length, 20);
  assert.strictEqual(win.hasMore, true);
  assert.strictEqual(win.total, 25);
  // 窗口内是这一天最新的 20 条
  assert.strictEqual(win.visible[0].title, '任务24');
  assert.strictEqual(win.visible[19].title, '任务5');
  // 默认 limit 就是 20
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

// ---------------------------------------------------------------- app.js 纯工具

test('formatDayHeader 渲染为 Mon 8/13 形式，绝无 ISO 片段', () => {
  assert.strictEqual(App.formatDayHeader(new Date(2026, 7, 13)), 'Thu 8/13');
  assert.strictEqual(App.formatDayHeader(new Date(2026, 7, 10)), 'Mon 8/10');
  assert.strictEqual(App.formatDayHeader(new Date(2026, 7, 16)), 'Sun 8/16');
  assert.strictEqual(App.formatDayHeader(new Date(2026, 0, 5)), 'Mon 1/5');
  assert.strictEqual(App.formatDayHeader(new Date(2026, 11, 25)), 'Fri 12/25');
  // 不允许出现 ISO 特征（连字符、零填充、T、冒号）
  assert.strictEqual(/[-T:]/.test(App.formatDayHeader(new Date(2026, 11, 25))), false);
});

test('toMonday 返回本周（周一起始）的周一', () => {
  const mon = App.toMonday(new Date(2026, 7, 13)); // 周四
  assert.strictEqual(mon.getFullYear(), 2026);
  assert.strictEqual(mon.getMonth(), 7);
  assert.strictEqual(mon.getDate(), 10);
  const sun = App.toMonday(new Date(2026, 7, 16)); // 周日 → 本周一
  assert.strictEqual(sun.getDate(), 10);
  const itself = App.toMonday(new Date(2026, 7, 10)); // 周一
  assert.strictEqual(itself.getDate(), 10);
  const jan1 = App.toMonday(new Date(2026, 0, 1)); // 周四 → 2025-12-29
  assert.strictEqual(jan1.getFullYear(), 2025);
  assert.strictEqual(jan1.getMonth(), 11);
  assert.strictEqual(jan1.getDate(), 29);
});

test('dayDates 从周一起连续 7 天', () => {
  const ds = App.dayDates(new Date(2026, 7, 13));
  assert.strictEqual(ds.length, 7);
  assert.strictEqual(App.formatDayHeader(ds[0]), 'Mon 8/10');
  assert.strictEqual(App.formatDayHeader(ds[6]), 'Sun 8/16');
});

test('adjacentDay 在周边界不越界', () => {
  assert.strictEqual(App.adjacentDay(2, 1), 3);
  assert.strictEqual(App.adjacentDay(2, -1), 1);
  assert.strictEqual(App.adjacentDay(0, -1), null);
  assert.strictEqual(App.adjacentDay(6, 1), null);
});

// ---------------------------------------------------------------- 汇总

console.log('');
console.log(passed + ' passed, ' + failed + ' failed');
if (failed > 0) {
  console.log('失败用例：');
  for (const f of failures) console.log('  - ' + f.name + ': ' + f.err.message);
  process.exitCode = 1;
}
