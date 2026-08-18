/* Ledger — app.js
 * UI logic for the Ledger personal finance tracker.
 * Zero dependencies; expects store.js to have loaded first (window.LedgerStore).
 */
(function () {
  'use strict';

  var S = window.LedgerStore;
  if (!S) return;

  var storage = null;
  try { storage = window.localStorage; } catch (e) { storage = null; }
  var store = S.createStore(storage);

  var currentView = 'dashboard';
  var filters = { month: 'all', categoryId: 'all', type: 'all', query: '' };
  var editingId = null;
  var confirmCallback = null;
  var lastCategoryId = null;

  var searchDebouncer = S.createDebouncer(200);   // ~200ms of typing silence
  var resizeDebouncer = S.createDebouncer(150);

  var CHART_INCOME = '#2e8b74';
  var CHART_EXPENSE = '#c2574a';
  var CHART_INK = '#6d7680';
  var CHART_GRID = '#e8e6df';
  var CHART_BASE = '#b9b6ac';

  function $(id) { return document.getElementById(id); }

  function todayISO() { return S.todayISO(); }
  function currentMonth() { return todayISO().slice(0, 7); }

  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  function announce(message) {
    var a = $('announcer');
    if (!a) return;
    a.textContent = '';
    setTimeout(function () { a.textContent = message; }, 60);
  }

  function signedAmount(entry) {
    var s = S.formatCents(entry.amountCents);
    return entry.type === 'income' ? '+' + s : '-' + s;
  }

  function sortedCategories() {
    var cats = store.getState().categories.slice();
    cats.sort(function (a, b) {
      if (a.id === S.UNCATEGORIZED_ID) return 1;
      if (b.id === S.UNCATEGORIZED_ID) return -1;
      return 0;
    });
    return cats;
  }

  function addOption(select, value, label, selected) {
    var opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    if (selected) opt.selected = true;
    select.appendChild(opt);
  }

  /* ----------------------------- rows ----------------------------- */

  function entryRow(entry, withEdit) {
    var li = document.createElement('li');
    li.className = 'entry-row ' + (entry.type === 'income' ? 'is-income' : 'is-expense');

    var main = document.createElement('div');
    main.className = 'entry-main';
    var title = document.createElement('span');
    title.className = 'entry-title';
    title.textContent = entry.title;
    var meta = document.createElement('span');
    meta.className = 'entry-meta';
    meta.textContent = store.categoryName(entry.categoryId) + ' \u00b7 ' + S.displayDate(entry.date);
    main.appendChild(title);
    main.appendChild(meta);

    var amt = document.createElement('span');
    amt.className = 'entry-amount';
    amt.textContent = signedAmount(entry);

    li.appendChild(main);
    li.appendChild(amt);

    if (withEdit) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn entry-edit';
      btn.textContent = 'Edit';
      btn.setAttribute('aria-label', 'Edit entry ' + entry.title);
      btn.addEventListener('click', function () { openEdit(entry.id); });
      li.appendChild(btn);
    }
    return li;
  }

  function emptyNote(text) {
    var li = document.createElement('li');
    li.className = 'empty-note';
    li.textContent = text;
    return li;
  }

  /* --------------------------- dashboard -------------------------- */

  function renderDashboard() {
    var ym = currentMonth();
    $('dashboard-month').textContent = S.monthLabel(ym);
    var t = store.totalsForMonth(ym);
    var balance = t.income - t.expenses;
    $('stat-income').textContent = S.formatCents(t.income);
    $('stat-expenses').textContent = S.formatCents(t.expenses);
    var bEl = $('stat-balance');
    bEl.textContent = S.formatCents(balance);
    bEl.classList.toggle('negative', balance < 0);

    var list = $('recent-list');
    list.textContent = '';
    var recent = store.sortedEntries().slice(0, 3);
    if (recent.length === 0) {
      list.appendChild(emptyNote('No entries yet \u2014 add your first entry.'));
      return;
    }
    for (var i = 0; i < recent.length; i++) list.appendChild(entryRow(recent[i], false));
  }

  /* ------------------------- transactions ------------------------- */

  function renderTxFilters() {
    var monthSel = $('filter-month');
    var prevMonth = filters.month;
    var months = store.months();
    monthSel.textContent = '';
    addOption(monthSel, 'all', 'All months', prevMonth === 'all' || months.indexOf(prevMonth) === -1);
    for (var i = 0; i < months.length; i++) {
      addOption(monthSel, months[i], S.monthLabel(months[i]), months[i] === prevMonth);
    }
    filters.month = monthSel.value;

    var catSel = $('filter-category');
    var prevCat = filters.categoryId;
    var cats = sortedCategories();
    catSel.textContent = '';
    addOption(catSel, 'all', 'All categories', prevCat === 'all' || !cats.some(function (c) { return c.id === prevCat; }));
    for (var j = 0; j < cats.length; j++) {
      addOption(catSel, cats[j].id, cats[j].name, cats[j].id === prevCat);
    }
    filters.categoryId = catSel.value;
  }

  function renderTxList() {
    var all = store.sortedEntries();
    var shown = S.filterEntries(all, filters);
    var list = $('tx-list');
    list.textContent = '';
    $('tx-count').textContent = 'Showing ' + plural(shown.length, 'entry', 'entries') + ' of ' + plural(all.length, 'entry', 'entries') + '.';
    if (shown.length === 0) {
      list.appendChild(emptyNote(all.length === 0 ? 'No entries yet \u2014 add your first entry.' : 'No entries match your filters.'));
      return;
    }
    for (var i = 0; i < shown.length; i++) list.appendChild(entryRow(shown[i], true));
  }

  /* ---------------------------- budgets --------------------------- */

  function renderBudgets() {
    $('budget-month-label').textContent = S.monthLabel(currentMonth());
    var state = store.getState();
    var list = $('budget-list');
    list.textContent = '';
    var ym = currentMonth();
    var cats = sortedCategories();
    for (var i = 0; i < cats.length; i++) {
      var cat = cats[i];
      var spent = S.sumEntries(S.filterEntries(state.entries, { month: ym, categoryId: cat.id, type: 'expense' }), 'expense');
      var budget = state.budgets[cat.id] || 0;
      var prog = S.budgetProgress(spent, budget);
      list.appendChild(budgetRow(cat, spent, budget, prog));
    }
    renderCatList();
  }

  function budgetRow(cat, spent, budget, prog) {
    var li = document.createElement('li');
    li.className = 'budget-row' + (prog.overspent ? ' overspent' : '');

    var head = document.createElement('div');
    head.className = 'budget-head';
    var name = document.createElement('span');
    name.className = 'budget-name';
    name.textContent = cat.name;
    var nums = document.createElement('span');
    nums.className = 'budget-nums';
    if (budget > 0) {
      nums.textContent = S.formatCents(spent) + ' of ' + S.formatCents(budget);
      if (prog.overspent) {
        var over = document.createElement('span');
        over.className = 'budget-over';
        over.textContent = ' \u2014 over by ' + S.formatCents(spent - budget);
        nums.appendChild(over);
      }
    } else {
      nums.textContent = S.formatCents(spent) + ' spent \u00b7 no budget set';
    }
    head.appendChild(name);
    head.appendChild(nums);
    li.appendChild(head);

    if (budget > 0) {
      // Native <progress>: value comes from attributes, so no inline styles
      // are needed, and screen readers get progressbar semantics for free.
      var track = document.createElement('progress');
      track.className = 'budget-track';
      track.max = 100;
      track.value = Math.round(prog.ratio * 100);
      track.setAttribute('aria-label', 'Budget progress for ' + cat.name);
      li.appendChild(track);
    }

    var edit = document.createElement('div');
    edit.className = 'budget-edit';
    var label = document.createElement('label');
    label.textContent = 'Budget';
    var input = document.createElement('input');
    input.type = 'text';
    input.inputMode = 'decimal';
    input.className = 'budget-input';
    input.value = budget > 0 ? S.formatCents(budget) : '';
    input.placeholder = '0.00';
    input.setAttribute('aria-label', 'Monthly budget for ' + cat.name);
    label.appendChild(input);
    var saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'btn';
    saveBtn.textContent = budget > 0 ? 'Update' : 'Set';
    saveBtn.addEventListener('click', function () {
      var cents = S.parseAmountToCents(input.value);
      if (cents === null) {
        announce('Enter a valid budget amount for ' + cat.name + '.');
        input.focus();
        return;
      }
      var res = store.setBudget(cat.id, cents);
      if (!res.ok) { announce(res.error); return; }
      announce('Budget for ' + cat.name + ' set to ' + S.formatCents(cents) + ' per month.');
      renderBudgets();
    });
    edit.appendChild(label);
    edit.appendChild(saveBtn);
    li.appendChild(edit);
    return li;
  }

  /* ------------------------ category management ------------------- */

  function renderCatList() {
    var list = $('cat-list');
    list.textContent = '';
    var cats = sortedCategories();
    for (var i = 0; i < cats.length; i++) list.appendChild(catRow(cats[i]));
  }

  function catRow(cat) {
    var li = document.createElement('li');
    li.className = 'cat-row';
    var name = document.createElement('span');
    name.className = 'cat-name';
    name.textContent = cat.name;
    li.appendChild(name);
    if (cat.id !== S.UNCATEGORIZED_ID) {
      var renameBtn = document.createElement('button');
      renameBtn.type = 'button';
      renameBtn.className = 'btn';
      renameBtn.textContent = 'Rename';
      renameBtn.addEventListener('click', function () { startRename(cat, li); });
      var delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'btn danger';
      delBtn.textContent = 'Delete';
      delBtn.addEventListener('click', function () {
        var count = 0;
        var entries = store.getState().entries;
        for (var i = 0; i < entries.length; i++) {
          if (entries[i].categoryId === cat.id) count += 1;
        }
        confirmDialog(
          'Delete category "' + cat.name + '"? ' + (count === 1 ? 'Its entry moves' : 'Its entries move') + ' to Uncategorized.',
          function () {
            var res = store.deleteCategory(cat.id);
            if (!res.ok) { announce(res.error); return; }
            announce('Deleted category ' + cat.name + '.');
            renderAll();
          }
        );
      });
      li.appendChild(renameBtn);
      li.appendChild(delBtn);
    }
    return li;
  }

  function startRename(cat, li) {
    li.textContent = '';
    var input = document.createElement('input');
    input.type = 'text';
    input.maxLength = 60;
    input.value = cat.name;
    input.className = 'cat-rename-input';
    input.setAttribute('aria-label', 'New name for category ' + cat.name);
    var save = document.createElement('button');
    save.type = 'button';
    save.className = 'btn';
    save.textContent = 'Save';
    var cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn';
    cancel.textContent = 'Cancel';
    save.addEventListener('click', function () { doRename(cat.id, input); });
    cancel.addEventListener('click', function () { renderBudgets(); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); doRename(cat.id, input); }
      if (e.key === 'Escape') { renderBudgets(); }
    });
    li.appendChild(input);
    li.appendChild(save);
    li.appendChild(cancel);
    input.focus();
    input.select();
  }

  function doRename(catId, input) {
    var res = store.renameCategory(catId, input.value);
    if (!res.ok) { announce(res.error); input.focus(); return; }
    announce('Category renamed to ' + res.category.name + '.');
    renderBudgets();
  }

  /* ----------------------------- charts --------------------------- */

  function shortMonthLabel(ym) {
    var names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    var m = parseInt(ym.slice(5, 7), 10);
    return names[m - 1] + ' ' + ym.slice(2, 4);
  }

  function renderChart() {
    var canvas = $('chart-canvas');
    if (!canvas) return;
    var wrap = canvas.parentElement;
    var dpr = window.devicePixelRatio || 1;
    var cssW = Math.max(240, wrap.clientWidth || 300);
    var cssH = 240;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    var ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var data = store.lastSixMonths();
    var padTop = 14;
    var padBottom = 26;
    var padX = 10;
    var chartW = cssW - padX * 2;
    var chartH = cssH - padTop - padBottom;
    var baseline = padTop + chartH;

    var maxVal = 0;
    for (var i = 0; i < data.length; i++) {
      if (data[i].income > maxVal) maxVal = data[i].income;
      if (data[i].expenses > maxVal) maxVal = data[i].expenses;
    }
    if (maxVal <= 0) maxVal = 1;

    ctx.strokeStyle = CHART_GRID;
    ctx.lineWidth = 1;
    for (var g = 1; g <= 3; g++) {
      var gy = baseline - (chartH * g) / 4;
      ctx.beginPath();
      ctx.moveTo(padX, gy);
      ctx.lineTo(cssW - padX, gy);
      ctx.stroke();
    }
    ctx.strokeStyle = CHART_BASE;
    ctx.beginPath();
    ctx.moveTo(padX, baseline);
    ctx.lineTo(cssW - padX, baseline);
    ctx.stroke();

    var groupW = chartW / data.length;
    var barW = Math.min(26, groupW * 0.32);
    var gap = barW * 0.35;
    for (var m = 0; m < data.length; m++) {
      var cx = padX + groupW * m + groupW / 2;
      var incomeH = (data[m].income / maxVal) * chartH;
      var expenseH = (data[m].expenses / maxVal) * chartH;
      ctx.fillStyle = CHART_INCOME;
      ctx.fillRect(cx - barW - gap / 2, baseline - incomeH, barW, incomeH);
      ctx.fillStyle = CHART_EXPENSE;
      ctx.fillRect(cx + gap / 2, baseline - expenseH, barW, expenseH);
      ctx.fillStyle = CHART_INK;
      ctx.font = '11px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(shortMonthLabel(data[m].month), cx, baseline + 8);
    }
  }

  /* -------------------------- entry dialog ------------------------ */

  function clearErrors() {
    $('entry-title-error').hidden = true;
    $('entry-amount-error').hidden = true;
    $('entry-date-error').hidden = true;
    $('entry-title').removeAttribute('aria-invalid');
    $('entry-amount').removeAttribute('aria-invalid');
    $('entry-date').removeAttribute('aria-invalid');
  }

  function showFieldError(inputId, errorId, message) {
    var input = $(inputId);
    var err = $(errorId);
    input.setAttribute('aria-invalid', 'true');
    err.textContent = message;
    err.hidden = false;
  }

  function fillCategorySelect(select, selectedId) {
    select.textContent = '';
    var cats = sortedCategories();
    for (var i = 0; i < cats.length; i++) {
      addOption(select, cats[i].id, cats[i].name, cats[i].id === selectedId);
    }
  }

  function openAdd() {
    editingId = null;
    $('entry-dialog-title').textContent = 'New entry';
    $('entry-delete').hidden = true;
    $('entry-type').value = 'expense';
    var state = store.getState();
    var cats = sortedCategories();
    var selected = lastCategoryId && state.categories.some(function (c) { return c.id === lastCategoryId; })
      ? lastCategoryId
      : (cats[0] ? cats[0].id : S.UNCATEGORIZED_ID);
    fillCategorySelect($('entry-category'), selected);
    $('entry-title').value = '';
    $('entry-amount').value = '';
    $('entry-date').value = todayISO();
    clearErrors();
    var dlg = $('entry-dialog');
    if (!dlg.open) dlg.showModal();
    $('entry-title').focus();
  }

  function openEdit(id) {
    var entry = null;
    var entries = store.getState().entries;
    for (var i = 0; i < entries.length; i++) {
      if (entries[i].id === id) { entry = entries[i]; break; }
    }
    if (!entry) return;
    editingId = id;
    $('entry-dialog-title').textContent = 'Edit entry';
    $('entry-delete').hidden = false;
    $('entry-type').value = entry.type;
    fillCategorySelect($('entry-category'), entry.categoryId);
    $('entry-title').value = entry.title;
    $('entry-amount').value = S.formatCents(entry.amountCents);
    $('entry-date').value = entry.date;
    clearErrors();
    var dlg = $('entry-dialog');
    if (!dlg.open) dlg.showModal();
    $('entry-title').focus();
  }

  function validateAmountField() {
    var v = $('entry-amount').value;
    if (v.trim() !== '' && S.parseAmountToCents(v) === null) {
      showFieldError('entry-amount', 'entry-amount-error', 'Enter a valid amount, like 12.34.');
    } else {
      $('entry-amount-error').hidden = true;
      $('entry-amount').removeAttribute('aria-invalid');
    }
  }

  function validateDateField() {
    var v = $('entry-date').value;
    if (v !== '' && !S.validateDate(v)) {
      showFieldError('entry-date', 'entry-date-error', 'Enter a valid date.');
    } else {
      $('entry-date-error').hidden = true;
      $('entry-date').removeAttribute('aria-invalid');
    }
  }

  /* --------------------------- confirm ---------------------------- */

  function confirmDialog(message, onConfirm) {
    $('confirm-text').textContent = message;
    confirmCallback = onConfirm;
    var dlg = $('confirm-dialog');
    if (!dlg.open) dlg.showModal();
  }

  /* ---------------------------- events ---------------------------- */

  function bindEvents() {
    var navBtns = document.querySelectorAll('.nav-btn');
    for (var nb = 0; nb < navBtns.length; nb++) {
      navBtns[nb].addEventListener('click', function () {
        switchView(this.getAttribute('data-view'));
      });
    }

    $('btn-add-entry').addEventListener('click', openAdd);
    $('entry-cancel').addEventListener('click', function () { $('entry-dialog').close(); });

    $('entry-title').addEventListener('input', function () {
      if ($('entry-title').value.trim() === '') {
        showFieldError('entry-title', 'entry-title-error', 'Title is required.');
      } else {
        $('entry-title-error').hidden = true;
        $('entry-title').removeAttribute('aria-invalid');
      }
    });
    $('entry-title').addEventListener('blur', function () {
      if ($('entry-title').value.trim() === '') {
        showFieldError('entry-title', 'entry-title-error', 'Title is required.');
      }
    });
    $('entry-amount').addEventListener('blur', validateAmountField);
    $('entry-date').addEventListener('blur', validateDateField);

    $('entry-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var title = $('entry-title').value;
      var amountStr = $('entry-amount').value;
      var date = $('entry-date').value;
      var type = $('entry-type').value;
      var categoryId = $('entry-category').value;

      var bad = false;
      if (title.trim() === '') { showFieldError('entry-title', 'entry-title-error', 'Title is required.'); bad = true; }
      var cents = S.parseAmountToCents(amountStr);
      if (cents === null) { showFieldError('entry-amount', 'entry-amount-error', 'Enter a valid amount, like 12.34.'); bad = true; }
      if (!S.validateDate(date)) { showFieldError('entry-date', 'entry-date-error', 'Enter a valid date.'); bad = true; }

      if (bad) {
        var first = $('entry-title').getAttribute('aria-invalid') ? $('entry-title')
          : ($('entry-amount').getAttribute('aria-invalid') ? $('entry-amount') : $('entry-date'));
        first.focus();
        return;
      }

      var fields = { type: type, title: title, amountCents: cents, date: date, categoryId: categoryId };
      var result = editingId === null ? store.addEntry(fields) : store.updateEntry(editingId, fields);
      if (!result.ok) {
        var msg = result.error || 'Could not save.';
        if (msg.indexOf('Title') !== -1) showFieldError('entry-title', 'entry-title-error', msg);
        else if (msg.indexOf('amount') !== -1) showFieldError('entry-amount', 'entry-amount-error', msg);
        else if (msg.indexOf('date') !== -1) showFieldError('entry-date', 'entry-date-error', msg);
        else announce(msg);
        return;
      }
      lastCategoryId = categoryId;
      $('entry-dialog').close();
      announce((editingId === null ? 'Added ' : 'Updated ') + type + ' "' + result.entry.title + '" \u2014 ' + S.formatCents(cents) + '.');
      renderAll();
    });

    $('entry-delete').addEventListener('click', function () {
      if (editingId === null) return;
      var id = editingId;
      var entry = null;
      var entries = store.getState().entries;
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].id === id) { entry = entries[i]; break; }
      }
      $('entry-dialog').close();
      confirmDialog('Delete "' + (entry ? entry.title : 'this entry') + '" permanently?', function () {
        var res = store.deleteEntry(id);
        if (!res.ok) { announce(res.error); return; }
        announce('Deleted entry "' + res.entry.title + '".');
        renderAll();
      });
    });

    $('confirm-cancel').addEventListener('click', function () {
      confirmCallback = null;
      $('confirm-dialog').close();
    });
    $('confirm-ok').addEventListener('click', function () {
      var cb = confirmCallback;
      confirmCallback = null;
      $('confirm-dialog').close();
      if (cb) cb();
    });

    $('cat-add-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var input = $('cat-add-name');
      var res = store.addCategory(input.value);
      if (!res.ok) { announce(res.error); input.focus(); return; }
      input.value = '';
      announce('Added category "' + res.category.name + '".');
      renderBudgets();
    });

    $('filter-month').addEventListener('change', function () { filters.month = this.value; renderTxList(); });
    $('filter-category').addEventListener('change', function () { filters.categoryId = this.value; renderTxList(); });
    $('filter-type').addEventListener('change', function () { filters.type = this.value; renderTxList(); });
    $('search-input').addEventListener('input', function () {
      searchDebouncer.call(function () {
        filters.query = $('search-input').value;
        renderTxList();
      });
    });

    $('btn-export').addEventListener('click', function () {
      var state = store.getState();
      if (state.entries.length === 0) {
        announce('Nothing to export yet.');
        return;
      }
      var csv = store.exportCSV();
      var blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = 'ledger-export-' + todayISO() + '.csv';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      announce('Exported ' + plural(state.entries.length, 'entry', 'entries') + ' to CSV.');
    });

    $('btn-import').addEventListener('click', function () {
      $('file-import').click();
    });
    $('file-import').addEventListener('change', function () {
      var input = this;
      var file = input.files && input.files[0];
      input.value = '';
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function () {
        var result = store.importCSV(String(reader.result || ''));
        if (result.ok) {
          var msg = 'Imported ' + plural(result.imported, 'entry', 'entries') + '.';
          if (result.skipped > 0) msg += ' Skipped ' + plural(result.skipped, 'malformed row', 'malformed rows') + '.';
          announce(msg);
          renderAll();
        } else {
          announce(result.error || 'Import failed. Your data is unchanged.');
        }
      };
      reader.onerror = function () {
        announce('Could not read the file. Your data is unchanged.');
      };
      reader.readAsText(file);
    });

    window.addEventListener('resize', function () {
      resizeDebouncer.call(function () {
        if (currentView === 'charts') renderChart();
      });
    });
  }

  function switchView(name) {
    currentView = name;
    var views = document.querySelectorAll('.view');
    for (var i = 0; i < views.length; i++) {
      views[i].hidden = views[i].getAttribute('data-view') !== name;
    }
    var navBtns = document.querySelectorAll('.nav-btn');
    for (var j = 0; j < navBtns.length; j++) {
      if (navBtns[j].getAttribute('data-view') === name) navBtns[j].setAttribute('aria-current', 'page');
      else navBtns[j].removeAttribute('aria-current');
    }
    if (name === 'dashboard') renderDashboard();
    else if (name === 'transactions') { renderTxFilters(); renderTxList(); }
    else if (name === 'budgets') renderBudgets();
    else if (name === 'charts') renderChart();
  }

  function renderAll() {
    renderDashboard();
    renderTxFilters();
    renderTxList();
    renderBudgets();
    if (currentView === 'charts') renderChart();
  }

  bindEvents();
  switchView('dashboard');
})();
