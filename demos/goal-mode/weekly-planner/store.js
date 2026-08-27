'use strict';

/**
 * store.js — pure data layer (no DOM dependency).
 * Responsibilities: add/move/delete tasks, title normalization
 * (trim + 120-char truncation), empty-title rejection, newest task first
 * within a day, localStorage persistence, corrupted-data recovery, and the
 * 20-per-column window logic.
 *
 * Exposed as the global WeeklyPlannerStore in browsers; exported via
 * module.exports in Node so test.js can require it directly.
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.WeeklyPlannerStore = factory(root);
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DAY_COUNT = 7; // 0=Mon ... 6=Sun
  var MAX_TITLE_LENGTH = 120;
  var DAY_WINDOW = 20; // default max tasks shown per column
  var STORAGE_KEY = 'weekly-planner.v1';

  /** Normalize day to an integer 0..6; return null when invalid. */
  function clampDay(day) {
    var n = Number(day);
    if (!isFinite(n) || Math.floor(n) !== n || n < 0 || n > DAY_COUNT - 1) {
      return null;
    }
    return n;
  }

  /** Normalize a title: trim whitespace; truncate past 120 chars in the stored data. */
  function normalizeTitle(raw) {
    if (typeof raw !== 'string') return '';
    return raw.trim().slice(0, MAX_TITLE_LENGTH);
  }

  /** Empty state: nextId auto-increments ids; days[dayIndex] = task array (newest first). */
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
   * Add a task (mutates state directly).
   * Returns { ok, task } or { ok:false, error: 'EMPTY_TITLE' | 'BAD_DAY' }.
   * Empty titles are rejected; no dedup/merge happens; the new task goes first that day.
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

  /** Move a task to another day; it lands first in the target day. */
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

  /** Delete a task. */
  function deleteTask(state, taskId) {
    var found = findTask(state, taskId);
    if (!found) return { ok: false, error: 'NOT_FOUND' };
    found.list.splice(found.index, 1);
    return { ok: true, task: found.task };
  }

  /** Return a copy of one day's tasks (newest first). */
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
   * Parse the raw localStorage string; any corruption falls back to a clean
   * empty state and never throws. Validates tasks one by one: invalid entries
   * are dropped, titles are re-normalized, and nextId is at least greater than
   * the largest existing id (so new task ids never collide).
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

  /** Save to storage; silently returns false when storage is unavailable (the app still runs in memory). */
  function saveState(storage, state) {
    if (!storage || typeof storage.setItem !== 'function') return false;
    try {
      storage.setItem(STORAGE_KEY, serialize(state));
      return true;
    } catch (err) {
      return false;
    }
  }

  /** Read from storage; missing or corrupted data returns a clean empty state. */
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
   * Per-day window logic: shows at most 20 (the newest 20) by default.
   * Returns { visible, hasMore, total }.
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
