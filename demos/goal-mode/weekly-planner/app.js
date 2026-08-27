'use strict';

/**
 * app.js — UI layer: seven-column Monday-to-Sunday layout, add/move/delete,
 * keyboard operation (arrow keys move tasks, Esc cancels delete confirmation),
 * visible focus, screen-reader roles/labels with aria-live announcements,
 * a 20-per-column window with show-more, delete double-confirmation,
 * and date formatting (Mon 8/13).
 *
 * Pure (date-related) helpers are exported behind a module.exports guard so
 * Node can require-test them directly; DOM initialization only runs in a browser.
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.WeeklyPlannerApp = factory(root);
  }
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  var WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  /** The Monday of the current week (week starts Monday). */
  function toMonday(date) {
    var d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    var dow = (d.getDay() + 6) % 7; // Monday = 0
    d.setDate(d.getDate() - dow);
    return d;
  }

  /** Dates render as 'Mon 8/13' - never an ISO string. */
  function formatDayHeader(date) {
    return (
      WEEKDAYS[date.getDay()] + ' ' + (date.getMonth() + 1) + '/' + date.getDate()
    );
  }

  /** 7 consecutive dates starting from Monday. */
  function dayDates(now) {
    var mon = toMonday(now);
    var out = [];
    for (var i = 0; i < 7; i++) {
      out.push(new Date(mon.getFullYear(), mon.getMonth(), mon.getDate() + i));
    }
    return out;
  }

  function isSameDay(a, b) {
    return (
      a.getFullYear() === b.getFullYear() &&
      a.getMonth() === b.getMonth() &&
      a.getDate() === b.getDate()
    );
  }

  /** Adjacent day (never out of range; returns null past the edges). */
  function adjacentDay(day, delta) {
    var d = day + delta;
    return d >= 0 && d <= 6 ? d : null;
  }

  /** Detect whether localStorage works (may throw under file:// or private mode). */
  function getStorage() {
    try {
      if (typeof localStorage === 'undefined' || localStorage === null) return null;
      var key = '__weekly_planner_probe__';
      localStorage.setItem(key, '1');
      localStorage.removeItem(key);
      return localStorage;
    } catch (err) {
      return null;
    }
  }

  function initApp(Store, opts) {
    if (!Store || !Store.createState) return;
    opts = opts || {};
    var storage = opts.storage !== undefined ? opts.storage : getStorage();
    var state = Store.loadState(storage);

    var liveRegion = document.getElementById('live-region');
    var cellEls = []; // per column: {form, input, errorEl, listEl, moreBtn, countEl, mcountEl}
    var expanded = {}; // day index -> true (fully expanded)
    var confirming = {}; // task id -> true (delete confirmation pending)
    var lastAddedId = null;

    function announce(message) {
      if (!liveRegion) return;
      liveRegion.textContent = '';
      void liveRegion.offsetWidth; // force reflow so repeated identical announcements re-read
      liveRegion.textContent = message;
    }

    function persist() {
      Store.saveState(storage, state);
    }

    function focusTask(taskId) {
      var el = document.querySelector('.task[data-id="' + taskId + '"]');
      if (el && el.focus) el.focus();
    }

    function dayOfTask(taskId) {
      for (var key in state.days) {
        if (!Object.prototype.hasOwnProperty.call(state.days, key)) continue;
        var list = state.days[key];
        for (var i = 0; i < list.length; i++) {
          if (list[i].id === taskId) return Number(key);
        }
      }
      return null;
    }

    /** Fill header dates and today markers; collect each column's DOM refs. */
    function buildRows() {
      var dates = dayDates(new Date());
      var today = new Date();
      for (var i = 0; i < 7; i++) {
        var head = document.getElementById('day-head-' + i);
        var dateEl = document.getElementById('day-date-' + i);
        var badgeEl = document.getElementById('day-today-' + i);
        var countEl = document.getElementById('day-count-' + i);
        var mdateEl = document.getElementById('day-mdate-' + i);
        var mbadgeEl = document.getElementById('day-mtoday-' + i);
        var mcountEl = document.getElementById('day-mcount-' + i);
        var cell = document.getElementById('day-cell-' + i);
        var form = document.getElementById('day-form-' + i);
        var input = document.getElementById('day-input-' + i);
        var errorEl = document.getElementById('day-error-' + i);
        var listEl = document.getElementById('day-list-' + i);
        var moreBtn = document.getElementById('day-more-' + i);

        var date = dates[i];
        var text = formatDayHeader(date);
        var label = DAY_LABELS[i] + ' ' + text;
        if (head) head.setAttribute('aria-label', label);
        if (cell) cell.setAttribute('aria-label', label + ' tasks');
        if (dateEl) dateEl.textContent = text;
        if (mdateEl) mdateEl.textContent = text;
        var isToday = isSameDay(date, today);
        if (badgeEl) badgeEl.hidden = !isToday;
        if (mbadgeEl) mbadgeEl.hidden = !isToday;
        if (head && isToday) head.classList.add('today');
        if (cell && isToday) cell.classList.add('today');

        cellEls[i] = {
          form: form,
          input: input,
          errorEl: errorEl,
          listEl: listEl,
          moreBtn: moreBtn,
          countEl: countEl,
          mcountEl: mcountEl
        };
      }
    }

    /** Render one day: task list (window), count, and the show-more button. */
    function renderDay(day) {
      var el = cellEls[day];
      if (!el || !el.listEl) return;
      var tasks = Store.getTasks(state, day);
      var win = Store.windowTasks(tasks, Store.DAY_WINDOW);
      var shown = expanded[day] ? tasks : win.visible;
      var countText = tasks.length ? tasks.length + ' items' : '';
      if (el.countEl) {
        el.countEl.textContent = countText;
        el.countEl.hidden = tasks.length === 0;
      }
      if (el.mcountEl) {
        el.mcountEl.textContent = countText;
        el.mcountEl.hidden = tasks.length === 0;
      }
      el.listEl.textContent = '';
      for (var i = 0; i < shown.length; i++) {
        el.listEl.appendChild(buildTaskRow(day, shown[i]));
      }
      if (!expanded[day] && win.hasMore) {
        el.moreBtn.textContent =
          'Show more (' + (win.total - win.visible.length) + ' left)';
        el.moreBtn.hidden = false;
      } else {
        el.moreBtn.hidden = true;
      }
    }

    function renderAll() {
      for (var i = 0; i < 7; i++) renderDay(i);
    }

    function moveTaskTo(fromDay, taskId, toDay) {
      if (toDay === null || toDay === fromDay) return;
      var res = Store.moveTask(state, taskId, toDay);
      if (!res.ok) return;
      persist();
      renderDay(fromDay);
      renderDay(toDay);
      announce('Moved \u201c' + res.task.title + '\u201d to ' + DAY_LABELS[toDay]);
      focusTask(taskId);
    }

    function enterConfirm(day, taskId) {
      confirming[taskId] = true;
      renderDay(day);
      var yes = document.querySelector('.confirm-yes[data-id="' + taskId + '"]');
      if (yes && yes.focus) yes.focus();
    }

    function cancelConfirm(taskId) {
      delete confirming[taskId];
      var day = dayOfTask(taskId);
      if (day !== null) renderDay(day);
      focusTask(taskId);
    }

    /** Build one task row. The row is focusable; left/right arrows move the task to an adjacent day. */
    function buildTaskRow(dayIndex, task) {
      var li = document.createElement('li');
      li.className = 'task' + (task.id === lastAddedId ? ' task-added' : '');
      li.setAttribute('role', 'listitem');
      li.tabIndex = 0;
      li.setAttribute('data-id', String(task.id));
      li.setAttribute('aria-label', 'Task: ' + task.title);

      var main = document.createElement('div');
      main.className = 'task-main';

      var title = document.createElement('span');
      title.className = 'task-title';
      title.textContent = task.title;
      main.appendChild(title);

      var controls = document.createElement('span');
      controls.className = 'task-controls';

      // Every task carries a small move-to-another-day control (a select)
      var select = document.createElement('select');
      select.className = 'move-select';
      select.setAttribute('aria-label', 'Move task to another day');
      for (var d = 0; d < 7; d++) {
        var opt = document.createElement('option');
        opt.value = String(d);
        opt.textContent = DAY_LABELS[d];
        if (d === dayIndex) opt.selected = true;
        select.appendChild(opt);
      }
      select.addEventListener('change', function () {
        moveTaskTo(dayIndex, task.id, Number(select.value));
      });
      controls.appendChild(select);

      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'delete-btn';
      del.textContent = 'Delete';
      del.setAttribute('aria-label', 'Delete task: ' + task.title);
      del.addEventListener('click', function () {
        enterConfirm(dayIndex, task.id);
      });
      controls.appendChild(del);

      main.appendChild(controls);
      li.appendChild(main);

      // When a task row has focus, left/right arrows move the task directly
      li.addEventListener('keydown', function (e) {
        var tag = e.target && e.target.tagName;
        if (tag === 'SELECT' || tag === 'INPUT' || tag === 'BUTTON' || tag === 'TEXTAREA') {
          return;
        }
        if (e.key === 'ArrowLeft') {
          e.preventDefault();
          moveTaskTo(dayIndex, task.id, Store.clampDay(dayIndex - 1));
        } else if (e.key === 'ArrowRight') {
          e.preventDefault();
          moveTaskTo(dayIndex, task.id, Store.clampDay(dayIndex + 1));
        }
      });

      // Delete double-confirmation
      if (confirming[task.id]) {
        li.classList.add('confirming');
        var bar = document.createElement('div');
        bar.className = 'confirm-bar';
        var msg = document.createElement('span');
        msg.className = 'confirm-msg';
        msg.textContent = 'Delete this task?';
        bar.appendChild(msg);

        var yes = document.createElement('button');
        yes.type = 'button';
        yes.className = 'confirm-yes';
        yes.setAttribute('data-id', String(task.id));
        yes.textContent = 'Delete';
        yes.addEventListener('click', function () {
          var res = Store.deleteTask(state, task.id);
          if (!res.ok) return;
          delete confirming[task.id];
          persist();
          renderDay(dayIndex);
          announce('Deleted task \u201c' + res.task.title + '\u201d');
          var first = cellEls[dayIndex].listEl.querySelector('.task');
          if (first) first.focus();
          else cellEls[dayIndex].input.focus();
        });
        bar.appendChild(yes);

        var no = document.createElement('button');
        no.type = 'button';
        no.className = 'confirm-no';
        no.textContent = 'Cancel';
        no.addEventListener('click', function () {
          cancelConfirm(task.id);
        });
        bar.appendChild(no);

        li.appendChild(bar);
      }

      return li;
    }

    /** Add a task from the input; empty titles show an inline error in place. */
    function addFromInput(day) {
      var el = cellEls[day];
      var res = Store.addTask(state, day, el.input.value);
      if (!res.ok) {
        if (res.error === 'EMPTY_TITLE') {
          el.errorEl.textContent = 'Task title cannot be empty';
          el.errorEl.hidden = false;
          el.input.focus();
        }
        return;
      }
      lastAddedId = res.task.id;
      el.errorEl.hidden = true;
      el.errorEl.textContent = '';
      el.input.value = '';
      persist();
      renderDay(day);
      announce('Added task \u201c' + res.task.title + '\u201d');
      el.input.focus();
    }

    function bindEvents() {
      for (var i = 0; i < 7; i++) {
        (function (day) {
          var el = cellEls[day];
          if (!el.form) return;
          el.form.addEventListener('submit', function (e) {
            e.preventDefault();
            addFromInput(day);
          });
          el.input.addEventListener('input', function () {
            if (!el.errorEl.hidden) {
              el.errorEl.hidden = true;
              el.errorEl.textContent = '';
            }
          });
          el.moreBtn.addEventListener('click', function () {
            expanded[day] = true;
            renderDay(day);
            announce('Showing all tasks');
            var first = el.listEl.querySelector('.task');
            if (first) first.focus();
            else el.input.focus();
          });
        })(i);
      }
      // Esc cancels the delete confirmation
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && Object.keys(confirming).length) {
          var ids = Object.keys(confirming);
          confirming = {};
          renderAll();
          if (ids.length) focusTask(Number(ids[0]));
        }
      });
    }

    buildRows();
    bindEvents();
    renderAll();
  }

  if (typeof document !== 'undefined' && document.getElementById) {
    function domReady() {
      var Store = root.WeeklyPlannerStore;
      if (Store) initApp(Store, {});
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', domReady);
    } else {
      domReady();
    }
  }

  return {
    WEEKDAYS: WEEKDAYS,
    DAY_LABELS: DAY_LABELS,
    toMonday: toMonday,
    formatDayHeader: formatDayHeader,
    dayDates: dayDates,
    isSameDay: isSameDay,
    adjacentDay: adjacentDay,
    initApp: initApp
  };
});
