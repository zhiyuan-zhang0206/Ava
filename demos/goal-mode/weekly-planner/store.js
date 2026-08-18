'use strict';

/**
 * store.js — 纯数据层（无 DOM 依赖）。
 * 职责：任务新增/移动/删除、标题规范化（trim + 120 字截断）、
 * 空标题拒绝、同日内最新任务在前、localStorage 持久化、
 * 损坏数据恢复、每列最多 20 条的窗口逻辑。
 *
 * 浏览器中暴露为全局 WeeklyPlannerStore；Node 中通过
 * module.exports 导出，供 test.js 直接 require 测试。
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.WeeklyPlannerStore = factory(root);
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DAY_COUNT = 7; // 0=周一 … 6=周日
  var MAX_TITLE_LENGTH = 120;
  var DAY_WINDOW = 20; // 每列默认最多显示的任务数
  var STORAGE_KEY = 'weekly-planner.v1';

  /** 把 day 规整为 0..6 的整数；非法返回 null。 */
  function clampDay(day) {
    var n = Number(day);
    if (!isFinite(n) || Math.floor(n) !== n || n < 0 || n > DAY_COUNT - 1) {
      return null;
    }
    return n;
  }

  /** 规范化标题：去除首尾空白；超过 120 字符在存储数据里直接截断。 */
  function normalizeTitle(raw) {
    if (typeof raw !== 'string') return '';
    return raw.trim().slice(0, MAX_TITLE_LENGTH);
  }

  /** 空状态：nextId 自增 id；days[dayIndex] = 任务数组（最新在前）。 */
  function createState() {
    return { nextId: 1, days: {} };
  }

  function dayList(state, day) {
    var key = String(day);
    if (
      !Object.prototype.hasOwnProperty.call(state.days, key) ||
      !Array.isArray(state.days[key])
    ) {
      state.days[key] = [];
    }
    return state.days[key];
  }

  function findTask(state, taskId) {
    for (var key in state.days) {
      if (!Object.prototype.hasOwnProperty.call(state.days, key)) continue;
      var list = state.days[key];
      for (var i = 0; i < list.length; i++) {
        if (list[i] && list[i].id === taskId) {
          return { list: list, index: i, task: list[i] };
        }
      }
    }
    return null;
  }

  /**
   * 新增任务（直接变更 state）。
   * 返回 { ok, task } 或 { ok:false, error: 'EMPTY_TITLE' | 'BAD_DAY' }。
   * 空标题拒绝；不做任何去重/合并；新任务排在当天最前。
   */
  function addTask(state, day, rawTitle) {
    var d = clampDay(day);
    if (d === null) return { ok: false, error: 'BAD_DAY' };
    var title = normalizeTitle(rawTitle);
    if (!title) return { ok: false, error: 'EMPTY_TITLE' };
    var task = { id: state.nextId, title: title, day: d, createdAt: Date.now() };
    state.nextId += 1;
    dayList(state, d).unshift(task);
    return { ok: true, task: task };
  }

  /** 把任务移动到另一天；目标天里它排在最前。 */
  function moveTask(state, taskId, toDay) {
    var d = clampDay(toDay);
    if (d === null) return { ok: false, error: 'BAD_DAY' };
    var found = findTask(state, taskId);
    if (!found) return { ok: false, error: 'NOT_FOUND' };
    if (found.task.day === d) return { ok: true, task: found.task, moved: false };
    found.list.splice(found.index, 1);
    found.task.day = d;
    dayList(state, d).unshift(found.task);
    return { ok: true, task: found.task, moved: true };
  }

  /** 删除任务。 */
  function deleteTask(state, taskId) {
    var found = findTask(state, taskId);
    if (!found) return { ok: false, error: 'NOT_FOUND' };
    found.list.splice(found.index, 1);
    return { ok: true, task: found.task };
  }

  /** 返回某天的任务副本（最新在前）。 */
  function getTasks(state, day) {
    var d = clampDay(day);
    if (d === null) return [];
    var list = state.days[String(d)];
    return list ? list.slice() : [];
  }

  function taskCount(state, day) {
    return getTasks(state, day).length;
  }

  function serialize(state) {
    return JSON.stringify({ nextId: state.nextId, days: state.days });
  }

  /**
   * 解析 localStorage 里的原始字符串；任何损坏都回退为干净的空状态，
   * 绝不抛出。逐条校验任务：非法条目丢弃，标题重新规范化，
   * nextId 至少大于现存最大 id（防止新任务 id 冲突）。
   */
  function parseStored(raw) {
    var fresh = createState();
    if (!raw) return fresh;
    var data;
    try {
      data = JSON.parse(raw);
    } catch (err) {
      return fresh;
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) return fresh;
    var nextId =
      typeof data.nextId === 'number' && isFinite(data.nextId) && data.nextId >= 1
        ? Math.floor(data.nextId)
        : 1;
    var days = {};
    var maxId = 0;
    var hasValidDays = data.days && typeof data.days === 'object' && !Array.isArray(data.days);
    for (var d = 0; d < DAY_COUNT; d++) {
      var key = String(d);
      var list = hasValidDays && Array.isArray(data.days[key]) ? data.days[key] : null;
      if (!list) continue;
      var clean = [];
      for (var i = 0; i < list.length; i++) {
        var t = list[i];
        if (!t || typeof t !== 'object' || Array.isArray(t)) continue;
        var id =
          typeof t.id === 'number' && isFinite(t.id) && Math.floor(t.id) === t.id
            ? t.id
            : null;
        var title = typeof t.title === 'string' ? normalizeTitle(t.title) : '';
        if (id === null || !title) continue;
        var createdAt =
          typeof t.createdAt === 'number' && isFinite(t.createdAt)
            ? t.createdAt
            : Date.now();
        if (id > maxId) maxId = id;
        clean.push({ id: id, title: title, day: d, createdAt: createdAt });
      }
      if (clean.length) days[key] = clean;
    }
    if (nextId <= maxId) nextId = maxId + 1;
    return { nextId: nextId, days: days };
  }

  /** 保存到 storage；storage 不可用时静默返回 false（应用仍可内存运行）。 */
  function saveState(storage, state) {
    if (!storage || typeof storage.setItem !== 'function') return false;
    try {
      storage.setItem(STORAGE_KEY, serialize(state));
      return true;
    } catch (err) {
      return false;
    }
  }

  /** 从 storage 读取；读不到或损坏都返回干净空状态。 */
  function loadState(storage) {
    if (!storage || typeof storage.getItem !== 'function') return createState();
    var raw = null;
    try {
      raw = storage.getItem(STORAGE_KEY);
    } catch (err) {
      return createState();
    }
    return parseStored(raw);
  }

  /**
   * 每日窗口逻辑：默认最多显示 20 条（最新的 20 条）。
   * 返回 { visible, hasMore, total }。
   */
  function windowTasks(list, limit) {
    var arr = Array.isArray(list) ? list : [];
    var n =
      typeof limit === 'number' && isFinite(limit) && limit >= 1
        ? Math.floor(limit)
        : DAY_WINDOW;
    return { visible: arr.slice(0, n), hasMore: arr.length > n, total: arr.length };
  }

  return {
    DAY_COUNT: DAY_COUNT,
    MAX_TITLE_LENGTH: MAX_TITLE_LENGTH,
    DAY_WINDOW: DAY_WINDOW,
    STORAGE_KEY: STORAGE_KEY,
    clampDay: clampDay,
    normalizeTitle: normalizeTitle,
    createState: createState,
    addTask: addTask,
    moveTask: moveTask,
    deleteTask: deleteTask,
    getTasks: getTasks,
    taskCount: taskCount,
    serialize: serialize,
    parseStored: parseStored,
    saveState: saveState,
    loadState: loadState,
    windowTasks: windowTasks
  };
});
