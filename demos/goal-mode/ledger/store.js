/* Ledger — store.js
 * Data layer for the Ledger personal finance tracker.
 *
 * Zero dependencies. Loads in the browser (exposes window.LedgerStore) and in
 * Node (module.exports) so the test suite can import the real functions.
 *
 * Money is stored as integer cents — never floats.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.LedgerStore = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var STORAGE_KEY = 'ledger.data.v1';
  var UNCATEGORIZED_ID = 'uncategorized';
  var STATE_VERSION = 1;

  var MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  var PRESET_CATEGORIES = [
    { id: 'cat-food', name: 'Food & Dining' },
    { id: 'cat-transport', name: 'Transport' },
    { id: 'cat-housing', name: 'Housing' },
    { id: 'cat-utilities', name: 'Utilities' },
    { id: 'cat-entertainment', name: 'Entertainment' },
    { id: 'cat-health', name: 'Health' },
    { id: 'cat-shopping', name: 'Shopping' },
    { id: 'cat-salary', name: 'Salary' },
    { id: 'cat-freelance', name: 'Freelance' }
  ];

  function uid() {
    return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  }

  function createInitialState() {
    return {
      version: STATE_VERSION,
      categories: [{ id: UNCATEGORIZED_ID, name: 'Uncategorized' }].concat(PRESET_CATEGORIES.map(function (c) {
        return { id: c.id, name: c.name };
      })),
      entries: [],
      budgets: {}
    };
  }

  /* ------------------------------ money ------------------------------ */

  // "12.34", "1,234.56", "45" -> integer cents. Anything else -> null.
  function parseAmountToCents(input) {
    if (typeof input !== 'string') return null;
    var s = input.trim();
    // Commas are only allowed as proper thousand separators: 1,234.56.
    if (!/^(\d{1,3}(,\d{3})*|\d+)(\.\d{1,2})?$/.test(s)) return null;
    s = s.replace(/,/g, '');
    var parts = s.split('.');
    var whole = parseInt(parts[0], 10);
    var frac = parts.length > 1 ? parseInt((parts[1] + '00').slice(0, 2), 10) : 0;
    var cents = whole * 100 + frac;
    if (!Number.isSafeInteger(cents) || cents <= 0) return null;
    return cents;
  }

  // 123456 -> "1,234.56". Negative -> "-1,234.56". Always exactly two decimals.
  function formatCents(cents) {
    if (typeof cents !== 'number' || !isFinite(cents)) return '0.00';
    var sign = cents < 0 ? '-' : '';
    var abs = Math.abs(Math.round(cents));
    var whole = Math.floor(abs / 100);
    var frac = abs % 100;
    var wholeStr = String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return sign + wholeStr + '.' + String(frac).padStart(2, '0');
  }

  /* ------------------------------ dates ------------------------------ */

  // Strict ISO "YYYY-MM-DD" plus calendar sanity (no Feb 30, etc.).
  function validateDate(iso) {
    if (typeof iso !== 'string') return false;
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
    if (!m) return false;
    var y = parseInt(m[1], 10);
    var mo = parseInt(m[2], 10);
    var d = parseInt(m[3], 10);
    if (mo < 1 || mo > 12 || d < 1 || d > 31) return false;
    var dt = new Date(Date.UTC(y, mo - 1, d));
    return dt.getUTCFullYear() === y && dt.getUTCMonth() === mo - 1 && dt.getUTCDate() === d;
  }

  function monthOf(iso) {
    return typeof iso === 'string' && iso.length >= 7 ? iso.slice(0, 7) : '';
  }

  function todayISO() {
    var d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  // "2026-08-13" -> "Aug 13" (year appended only when it is not the current year).
  function displayDate(iso) {
    if (!validateDate(iso)) return '';
    var y = parseInt(iso.slice(0, 4), 10);
    var m = parseInt(iso.slice(5, 7), 10);
    var d = parseInt(iso.slice(8, 10), 10);
    var label = MONTH_NAMES[m - 1] + ' ' + d;
    var now = new Date();
    return now.getFullYear() === y ? label : label + ', ' + y;
  }

  // "2026-08" -> "Aug 2026"
  function monthLabel(ym) {
    if (typeof ym !== 'string' || !/^\d{4}-\d{2}$/.test(ym)) return ym || '';
    var y = parseInt(ym.slice(0, 4), 10);
    var m = parseInt(ym.slice(5, 7), 10);
    if (m < 1 || m > 12) return ym;
    return MONTH_NAMES[m - 1] + ' ' + y;
  }

  /* ----------------------------- entries ----------------------------- */

  // Newest first: date desc, then id desc (ids are unique, so ties are stable).
  function sortEntries(entries) {
    return entries.slice().sort(function (a, b) {
      if (a.date !== b.date) return a.date < b.date ? 1 : -1;
      return a.id < b.id ? 1 : a.id > b.id ? -1 : 0;
    });
  }

  function filterEntries(entries, opts) {
    opts = opts || {};
    var out = entries;
    if (opts.month && opts.month !== 'all') {
      out = out.filter(function (e) { return e.date.slice(0, 7) === opts.month; });
    }
    if (opts.categoryId && opts.categoryId !== 'all') {
      out = out.filter(function (e) { return e.categoryId === opts.categoryId; });
    }
    if (opts.type && opts.type !== 'all') {
      out = out.filter(function (e) { return e.type === opts.type; });
    }
    if (opts.query) {
      var q = String(opts.query).trim().toLowerCase();
      if (q) {
        out = out.filter(function (e) { return e.title.toLowerCase().indexOf(q) !== -1; });
      }
    }
    return out;
  }

  function sumEntries(entries, type) {
    var total = 0;
    for (var i = 0; i < entries.length; i++) {
      var e = entries[i];
      if (!type || e.type === type) {
        if (typeof e.amountCents === 'number' && isFinite(e.amountCents)) {
          total += e.amountCents;
        }
      }
    }
    return total;
  }

  /* ----------------------------- debounce ---------------------------- */

  // Pure debouncer; timers can be injected so tests drive a fake clock.
  function createDebouncer(waitMs, timers) {
    // Call the globals through closures: extracting window.setTimeout breaks
    // its `this` binding in browsers ("Illegal invocation").
    timers = timers || {
      setTimeout: function (fn, ms) { return setTimeout(fn, ms); },
      clearTimeout: function (id) { return clearTimeout(id); }
    };
    var timer = null;
    var pending = null;
    return {
      call: function (fn) {
        pending = fn;
        if (timer !== null) timers.clearTimeout(timer);
        timer = timers.setTimeout(function () {
          timer = null;
          var f = pending;
          pending = null;
          if (typeof f === 'function') f();
        }, waitMs);
      },
      flush: function () {
        if (timer !== null) { timers.clearTimeout(timer); timer = null; }
        var f = pending;
        pending = null;
        if (typeof f === 'function') f();
      },
      cancel: function () {
        if (timer !== null) { timers.clearTimeout(timer); timer = null; }
        pending = null;
      },
      isPending: function () { return timer !== null; }
    };
  }

  /* ----------------------------- budgets ----------------------------- */

  // Ratio is clamped to [0, 1] — the bar never overflows. Overspent when spent > budget.
  function budgetProgress(spentCents, budgetCents) {
    if (typeof budgetCents !== 'number' || !isFinite(budgetCents) || budgetCents <= 0) {
      return { ratio: 0, overspent: false };
    }
    if (typeof spentCents !== 'number' || !isFinite(spentCents) || spentCents <= 0) {
      return { ratio: 0, overspent: false };
    }
    var ratio = Math.min(1, spentCents / budgetCents);
    return { ratio: ratio, overspent: spentCents > budgetCents };
  }

  /* ------------------------------- CSV ------------------------------- */

  var CSV_HEADER = ['id', 'type', 'title', 'amount', 'date', 'categoryId', 'categoryName'];

  // RFC-4180 escaping: quote fields containing commas, quotes, or newlines;
  // double embedded quotes.
  function csvEscape(value) {
    var s = String(value);
    if (/[",\r\n]/.test(s)) {
      return '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  }

  function csvRow(fields) {
    var out = [];
    for (var i = 0; i < fields.length; i++) out.push(csvEscape(fields[i]));
    return out.join(',');
  }

  function csvSerialize(entries, categoriesById) {
    var lines = [csvRow(CSV_HEADER)];
    for (var i = 0; i < entries.length; i++) {
      var e = entries[i];
      var cat = categoriesById ? categoriesById[e.categoryId] : null;
      lines.push(csvRow([
        e.id,
        e.type,
        e.title,
        formatCents(e.amountCents),
        e.date,
        e.categoryId,
        cat ? cat.name : ''
      ]));
    }
    return lines.join('\r\n') + '\r\n';
  }

  // RFC-4180-ish parser: quoted fields may contain commas, doubled quotes,
  // and CR/LF. Returns an array of row arrays (all strings). Never throws.
  function csvParse(text) {
    if (typeof text !== 'string') return [];
    var rows = [];
    var row = [];
    var field = '';
    var inQuotes = false;
    var i = 0;
    var n = text.length;
    while (i < n) {
      var ch = text[i];
      if (inQuotes) {
        if (ch === '"') {
          if (text[i + 1] === '"') { field += '"'; i += 2; }
          else { inQuotes = false; i += 1; }
        } else {
          field += ch;
          i += 1;
        }
      } else if (ch === '"' && field === '') {
        inQuotes = true;
        i += 1;
      } else if (ch === ',') {
        row.push(field);
        field = '';
        i += 1;
      } else if (ch === '\r') {
        if (text[i + 1] === '\n') i += 1;
        row.push(field);
        field = '';
        rows.push(row);
        row = [];
        i += 1;
      } else if (ch === '\n') {
        row.push(field);
        field = '';
        rows.push(row);
        row = [];
        i += 1;
      } else {
        field += ch;
        i += 1;
      }
    }
    if (field !== '' || row.length > 0) {
      row.push(field);
      rows.push(row);
    }
    return rows;
  }

  /* ---------------------------- sanitizing --------------------------- */

  // Validate and repair data that came from localStorage. Returns null when
  // the shape is fundamentally broken; the caller then starts fresh.
  function sanitizeState(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
    if (!Array.isArray(raw.entries) || !Array.isArray(raw.categories)) return null;

    var state = {
      version: STATE_VERSION,
      categories: [],
      entries: [],
      budgets: {}
    };
    var seen = {};

    for (var i = 0; i < raw.categories.length; i++) {
      var c = raw.categories[i];
      if (!c || typeof c !== 'object') continue;
      var cid = typeof c.id === 'string' ? c.id : '';
      var cname = typeof c.name === 'string' ? c.name.trim() : '';
      if (!cid || !cname || seen[cid]) continue;
      seen[cid] = true;
      state.categories.push({ id: cid, name: cname });
    }

    if (state.categories.length === 0) return null;
    if (!seen[UNCATEGORIZED_ID]) {
      state.categories.unshift({ id: UNCATEGORIZED_ID, name: 'Uncategorized' });
      seen[UNCATEGORIZED_ID] = true;
    }

    var entrySeen = {};
    for (var j = 0; j < raw.entries.length; j++) {
      var e = raw.entries[j];
      if (!e || typeof e !== 'object') continue;
      var eid = typeof e.id === 'string' && e.id ? e.id : null;
      var type = e.type === 'income' || e.type === 'expense' ? e.type : null;
      var title = typeof e.title === 'string' ? e.title : null;
      if (eid === null || type === null || title === null || title.trim() === '' || entrySeen[eid]) continue;
      var amount = typeof e.amountCents === 'number' && Number.isInteger(e.amountCents) && e.amountCents > 0 ? e.amountCents : null;
      if (amount === null) continue;
      var date = typeof e.date === 'string' && validateDate(e.date) ? e.date : null;
      if (date === null) continue;
      var catId = typeof e.categoryId === 'string' && seen[e.categoryId] ? e.categoryId : UNCATEGORIZED_ID;
      entrySeen[eid] = true;
      state.entries.push({ id: eid, type: type, title: title, amountCents: amount, date: date, categoryId: catId });
    }

    if (raw.budgets && typeof raw.budgets === 'object' && !Array.isArray(raw.budgets)) {
      for (var k in raw.budgets) {
        if (Object.prototype.hasOwnProperty.call(raw.budgets, k) && seen[k]) {
          var b = raw.budgets[k];
          if (typeof b === 'number' && Number.isInteger(b) && b > 0) state.budgets[k] = b;
        }
      }
    }

    return state;
  }

  /* ------------------------------ store ------------------------------ */

  function createStore(storage) {
    var state = null;

    function save() {
      if (!storage || typeof storage.setItem !== 'function') return true;
      try {
        storage.setItem(STORAGE_KEY, JSON.stringify(state));
        return true;
      } catch (e) {
        return false;
      }
    }

    function init() {
      var s = null;
      if (storage && typeof storage.getItem === 'function') {
        try {
          var raw = storage.getItem(STORAGE_KEY);
          if (raw != null) s = sanitizeState(JSON.parse(raw));
        } catch (e) {
          s = null;
        }
      }
      state = s || createInitialState();
      if (!s) save(); // heal missing/corrupt storage
    }

    function withRollback(mutate) {
      var snapshot = JSON.stringify(state);
      mutate();
      if (!save()) {
        state = JSON.parse(snapshot);
        return false;
      }
      return true;
    }

    function hasCategory(id) {
      for (var i = 0; i < state.categories.length; i++) {
        if (state.categories[i].id === id) return true;
      }
      return false;
    }

    function categoryName(id) {
      for (var i = 0; i < state.categories.length; i++) {
        if (state.categories[i].id === id) return state.categories[i].name;
      }
      return 'Uncategorized';
    }

    function validateEntry(fields) {
      if (!fields || typeof fields !== 'object') return 'Invalid entry.';
      if (fields.type !== 'income' && fields.type !== 'expense') return 'Choose a type: income or expense.';
      if (typeof fields.title !== 'string' || fields.title.trim() === '') return 'Title is required.';
      if (!Number.isInteger(fields.amountCents) || fields.amountCents <= 0) return 'Enter an amount greater than zero.';
      if (!validateDate(fields.date)) return 'Enter a valid date (YYYY-MM-DD).';
      if (typeof fields.categoryId !== 'string' || !hasCategory(fields.categoryId)) return 'Choose a category.';
      return null;
    }

    function findEntryIndex(id) {
      for (var i = 0; i < state.entries.length; i++) {
        if (state.entries[i].id === id) return i;
      }
      return -1;
    }

    function addEntry(fields) {
      var err = validateEntry(fields);
      if (err) return { ok: false, error: err };
      var entry = {
        id: uid(),
        type: fields.type,
        title: fields.title.trim(),
        amountCents: fields.amountCents,
        date: fields.date,
        categoryId: fields.categoryId
      };
      if (!withRollback(function () { state.entries.push(entry); })) {
        return { ok: false, error: 'Could not save. Your data is unchanged.' };
      }
      return { ok: true, entry: entry };
    }

    function updateEntry(id, fields) {
      var idx = findEntryIndex(id);
      if (idx === -1) return { ok: false, error: 'Entry not found.' };
      var old = state.entries[idx];
      var merged = {
        id: id,
        type: fields && fields.type !== undefined ? fields.type : old.type,
        title: fields && fields.title !== undefined ? fields.title : old.title,
        amountCents: fields && fields.amountCents !== undefined ? fields.amountCents : old.amountCents,
        date: fields && fields.date !== undefined ? fields.date : old.date,
        categoryId: fields && fields.categoryId !== undefined ? fields.categoryId : old.categoryId
      };
      var err = validateEntry(merged);
      if (err) return { ok: false, error: err };
      if (!withRollback(function () { state.entries[idx] = merged; })) {
        return { ok: false, error: 'Could not save. Your data is unchanged.' };
      }
      return { ok: true, entry: merged };
    }

    function deleteEntry(id) {
      var idx = findEntryIndex(id);
      if (idx === -1) return { ok: false, error: 'Entry not found.' };
      var removed = state.entries[idx];
      if (!withRollback(function () { state.entries.splice(idx, 1); })) {
        return { ok: false, error: 'Could not save. Your data is unchanged.' };
      }
      return { ok: true, entry: removed };
    }

    function addCategory(name) {
      if (typeof name !== 'string' || name.trim() === '') {
        return { ok: false, error: 'Category name is required.' };
      }
      var cat = { id: 'cat-' + uid(), name: name.trim() };
      if (!withRollback(function () { state.categories.push(cat); })) {
        return { ok: false, error: 'Could not save. Your data is unchanged.' };
      }
      return { ok: true, category: cat };
    }

    function renameCategory(id, name) {
      if (typeof name !== 'string' || name.trim() === '') {
        return { ok: false, error: 'Category name is required.' };
      }
      var idx = -1;
      for (var i = 0; i < state.categories.length; i++) {
        if (state.categories[i].id === id) { idx = i; break; }
      }
      if (idx === -1) return { ok: false, error: 'Category not found.' };
      var renamed = { id: id, name: name.trim() };
      if (!withRollback(function () { state.categories[idx] = renamed; })) {
        return { ok: false, error: 'Could not save. Your data is unchanged.' };
      }
      return { ok: true, category: renamed };
    }

    function deleteCategory(id) {
      if (id === UNCATEGORIZED_ID) {
        return { ok: false, error: 'The uncategorized category cannot be deleted.' };
      }
      var idx = -1;
      for (var i = 0; i < state.categories.length; i++) {
        if (state.categories[i].id === id) { idx = i; break; }
      }
      if (idx === -1) return { ok: false, error: 'Category not found.' };
      var done = withRollback(function () {
        state.categories.splice(idx, 1);
        for (var j = 0; j < state.entries.length; j++) {
          if (state.entries[j].categoryId === id) state.entries[j].categoryId = UNCATEGORIZED_ID;
        }
        delete state.budgets[id];
      });
      if (!done) return { ok: false, error: 'Could not save. Your data is unchanged.' };
      return { ok: true };
    }

    function setBudget(categoryId, cents) {
      if (!hasCategory(categoryId)) return { ok: false, error: 'Category not found.' };
      if (cents !== null && cents !== undefined) {
        if (!Number.isInteger(cents) || cents < 0) return { ok: false, error: 'Budget must be a positive amount.' };
      }
      var done = withRollback(function () {
        if (cents === null || cents === undefined || cents === 0) delete state.budgets[categoryId];
        else state.budgets[categoryId] = cents;
      });
      if (!done) return { ok: false, error: 'Could not save. Your data is unchanged.' };
      return { ok: true };
    }

    function months() {
      var set = {};
      for (var i = 0; i < state.entries.length; i++) {
        var m = state.entries[i].date.slice(0, 7);
        if (m.length === 7) set[m] = true;
      }
      return Object.keys(set).sort().reverse();
    }

    function totalsForMonth(ym) {
      var income = 0;
      var expenses = 0;
      for (var i = 0; i < state.entries.length; i++) {
        var e = state.entries[i];
        if (e.date.slice(0, 7) !== ym) continue;
        if (e.type === 'income') income += e.amountCents;
        else expenses += e.amountCents;
      }
      return { income: income, expenses: expenses };
    }

    function lastSixMonths() {
      var out = [];
      var now = new Date();
      for (var i = 5; i >= 0; i--) {
        var d = new Date(now.getFullYear(), now.getMonth() - i, 1);
        var ym = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
        var t = totalsForMonth(ym);
        out.push({ month: ym, income: t.income, expenses: t.expenses });
      }
      return out;
    }

    function exportCSV() {
      var byId = {};
      state.categories.forEach(function (c) { byId[c.id] = c; });
      return csvSerialize(state.entries, byId);
    }

    function importCSV(text) {
      var snapshot = JSON.stringify(state);
      var result = importCSVRows(text, state);
      if (result.ok && !save()) {
        state = JSON.parse(snapshot);
        return { ok: false, imported: 0, skipped: result.skipped, error: 'Could not save. Your data is unchanged.' };
      }
      return result;
    }

    init();

    return {
      getState: function () { return state; },
      categoryName: categoryName,
      sortedEntries: function () { return sortEntries(state.entries); },
      validateEntry: validateEntry,
      addEntry: addEntry,
      updateEntry: updateEntry,
      deleteEntry: deleteEntry,
      addCategory: addCategory,
      renameCategory: renameCategory,
      deleteCategory: deleteCategory,
      setBudget: setBudget,
      months: months,
      totalsForMonth: totalsForMonth,
      lastSixMonths: lastSixMonths,
      exportCSV: exportCSV,
      importCSV: importCSV
    };
  }

  // Shared by createStore().importCSV. Only mutates `state` on success, and
  // only after every row has been validated — a bad file can never wipe data.
  function importCSVRows(text, state) {
    if (typeof text !== 'string' || text.trim() === '') {
      return { ok: false, imported: 0, skipped: 0, error: 'The file is empty. Nothing was imported.' };
    }
    var rows = csvParse(text);
    if (rows.length === 0) {
      return { ok: false, imported: 0, skipped: 0, error: 'The file has no rows. Nothing was imported.' };
    }

    var start = 0;
    var h = rows[0];
    if (h.length >= 6 && h[0] === 'id' && h[1] === 'type' && h[2] === 'title' && h[3] === 'amount' && h[4] === 'date' && h[5] === 'categoryId') {
      start = 1;
    }

    var existingIds = {};
    state.entries.forEach(function (e) { existingIds[e.id] = true; });
    var categoriesById = {};
    state.categories.forEach(function (c) { categoriesById[c.id] = c; });

    var toAdd = [];
    var newCategories = [];
    var skipped = 0;

    for (var r = start; r < rows.length; r++) {
      var row = rows[r];
      if (!row || row.length === 0) { skipped += 1; continue; }
      if (row.length === 1 && String(row[0]).trim() === '') { skipped += 1; continue; }
      if (row.length < 6) { skipped += 1; continue; }

      var id = String(row[0]).trim();
      var type = String(row[1]).trim();
      var title = String(row[2]);
      var amountStr = String(row[3]).trim();
      var date = String(row[4]).trim();
      var categoryId = String(row[5]).trim();
      var categoryNameField = row.length > 6 ? String(row[6]).trim() : '';

      var bad = false;
      if (!id || existingIds[id]) bad = true;
      if (!bad && type !== 'income' && type !== 'expense') bad = true;
      if (!bad && title.trim() === '') bad = true;
      var amountCents = bad ? null : parseAmountToCents(amountStr);
      if (!bad && amountCents === null) bad = true;
      if (!bad && !validateDate(date)) bad = true;
      if (bad) { skipped += 1; continue; }

      // Unknown category id -> recreate the category (by id) so entries keep
      // their identity through a round trip. Uncategorized is built-in.
      if (categoryId !== UNCATEGORIZED_ID && !categoriesById[categoryId]) {
        var created = null;
        for (var ci = 0; ci < newCategories.length; ci++) {
          if (newCategories[ci].id === categoryId) { created = newCategories[ci]; break; }
        }
        if (!created) {
          created = { id: categoryId, name: categoryNameField || 'Imported' };
          newCategories.push(created);
          categoriesById[categoryId] = created;
        }
      }

      toAdd.push({ id: id, type: type, title: title, amountCents: amountCents, date: date, categoryId: categoryId });
      existingIds[id] = true;
    }

    if (toAdd.length === 0) {
      return { ok: false, imported: 0, skipped: skipped, error: 'No valid rows found. Nothing was imported — your data is unchanged.' };
    }

    for (var n = 0; n < newCategories.length; n++) state.categories.push(newCategories[n]);
    for (var m = 0; m < toAdd.length; m++) state.entries.push(toAdd[m]);
    return { ok: true, imported: toAdd.length, skipped: skipped, error: null };
  }

  return {
    STORAGE_KEY: STORAGE_KEY,
    UNCATEGORIZED_ID: UNCATEGORIZED_ID,
    PRESET_CATEGORIES: PRESET_CATEGORIES,
    createInitialState: createInitialState,
    createStore: createStore,
    parseAmountToCents: parseAmountToCents,
    formatCents: formatCents,
    validateDate: validateDate,
    monthOf: monthOf,
    todayISO: todayISO,
    displayDate: displayDate,
    monthLabel: monthLabel,
    sortEntries: sortEntries,
    filterEntries: filterEntries,
    sumEntries: sumEntries,
    createDebouncer: createDebouncer,
    budgetProgress: budgetProgress,
    csvEscape: csvEscape,
    csvParse: csvParse,
    csvSerialize: csvSerialize,
    sanitizeState: sanitizeState
  };
});
