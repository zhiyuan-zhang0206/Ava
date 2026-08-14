'use strict';

/**
 * app.js — 界面层：周一到周日七列布局、添加/移动/删除、
 * 键盘操作（方向键移动任务、Esc 取消删除确认）、可见焦点、
 * 屏幕阅读器角色/标签与 aria-live 播报、每列最多 20 条 + 显示更多、
 * 删除二次确认、日期格式化（Mon 8/13）。
 *
 * 纯工具函数（日期相关）通过 module.exports 守卫导出，Node 可直接
 * require 测试；DOM 初始化只在浏览器里运行。
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
  var DAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

  /** 本周（周一起始）的周一。 */
  function toMonday(date) {
    var d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    var dow = (d.getDay() + 6) % 7; // 周一 = 0
    d.setDate(d.getDate() - dow);
    return d;
  }

  /** 日期显示为 'Mon 8/13' 这种形式，绝不出现 ISO 字符串。 */
  function formatDayHeader(date) {
    return (
      WEEKDAYS[date.getDay()] + ' ' + (date.getMonth() + 1) + '/' + date.getDate()
    );
  }

  /** 从周一起连续 7 天的日期数组。 */
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

  /** 相邻天（不越界，越界返回 null）。 */
  function adjacentDay(day, delta) {
    var d = day + delta;
    return d >= 0 && d <= 6 ? d : null;
  }

  /** 探测 localStorage 是否可用（file:// 或隐私模式下可能抛错）。 */
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
    var cellEls = []; // 每列：{form, input, errorEl, listEl, moreBtn, countEl, mcountEl}
    var expanded = {}; // 天索引 -> true（该天已展开全部）
    var confirming = {}; // 任务 id -> true（删除确认中）
    var lastAddedId = null;

    function announce(message) {
      if (!liveRegion) return;
      liveRegion.textContent = '';
      void liveRegion.offsetWidth; // 强制重排，连续相同播报也会重新朗读
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

    /** 填充表头日期、今天标记，收集每列的 DOM 引用。 */
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
        if (cell) cell.setAttribute('aria-label', label + ' 的任务');
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

    /** 渲染某一天：任务列表（窗口）、数量、显示更多按钮。 */
    function renderDay(day) {
      var el = cellEls[day];
      if (!el || !el.listEl) return;
      var tasks = Store.getTasks(state, day);
      var win = Store.windowTasks(tasks, Store.DAY_WINDOW);
      var shown = expanded[day] ? tasks : win.visible;
      var countText = tasks.length ? tasks.length + ' 项' : '';
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
          '显示更多（还剩 ' + (win.total - win.visible.length) + ' 条）';
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
      announce('已把任务「' + res.task.title + '」移动到' + DAY_LABELS[toDay]);
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

    /** 构建一行任务。任务行可聚焦；左右方向键把任务移到相邻天。 */
    function buildTaskRow(dayIndex, task) {
      var li = document.createElement('li');
      li.className = 'task' + (task.id === lastAddedId ? ' task-added' : '');
      li.setAttribute('role', 'listitem');
      li.tabIndex = 0;
      li.setAttribute('data-id', String(task.id));
      li.setAttribute('aria-label', '任务：' + task.title);

      var main = document.createElement('div');
      main.className = 'task-main';

      var title = document.createElement('span');
      title.className = 'task-title';
      title.textContent = task.title;
      main.appendChild(title);

      var controls = document.createElement('span');
      controls.className = 'task-controls';

      // 每个任务都带一个移动到另一天的小控件（下拉选择）
      var select = document.createElement('select');
      select.className = 'move-select';
      select.setAttribute('aria-label', '移动任务到另一天');
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
      del.textContent = '删除';
      del.setAttribute('aria-label', '删除任务：' + task.title);
      del.addEventListener('click', function () {
        enterConfirm(dayIndex, task.id);
      });
      controls.appendChild(del);

      main.appendChild(controls);
      li.appendChild(main);

      // 任务行获得焦点时，左右方向键直接移动任务
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

      // 删除二次确认
      if (confirming[task.id]) {
        li.classList.add('confirming');
        var bar = document.createElement('div');
        bar.className = 'confirm-bar';
        var msg = document.createElement('span');
        msg.className = 'confirm-msg';
        msg.textContent = '确认删除？';
        bar.appendChild(msg);

        var yes = document.createElement('button');
        yes.type = 'button';
        yes.className = 'confirm-yes';
        yes.setAttribute('data-id', String(task.id));
        yes.textContent = '删除';
        yes.addEventListener('click', function () {
          var res = Store.deleteTask(state, task.id);
          if (!res.ok) return;
          delete confirming[task.id];
          persist();
          renderDay(dayIndex);
          announce('已删除任务「' + res.task.title + '」');
          var first = cellEls[dayIndex].listEl.querySelector('.task');
          if (first) first.focus();
          else cellEls[dayIndex].input.focus();
        });
        bar.appendChild(yes);

        var no = document.createElement('button');
        no.type = 'button';
        no.className = 'confirm-no';
        no.textContent = '取消';
        no.addEventListener('click', function () {
          cancelConfirm(task.id);
        });
        bar.appendChild(no);

        li.appendChild(bar);
      }

      return li;
    }

    /** 从输入框添加任务；空标题就地显示行内错误。 */
    function addFromInput(day) {
      var el = cellEls[day];
      var res = Store.addTask(state, day, el.input.value);
      if (!res.ok) {
        if (res.error === 'EMPTY_TITLE') {
          el.errorEl.textContent = '任务不能为空';
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
      announce('已添加任务「' + res.task.title + '」');
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
            announce('已显示全部任务');
            var first = el.listEl.querySelector('.task');
            if (first) first.focus();
            else el.input.focus();
          });
        })(i);
      }
      // Esc 取消删除确认
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
