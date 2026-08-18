/* global document, window */
/* Snake — the full arcade package. Zero dependencies.
   Pure game logic (testable in Node) + a small DOM app.
   Node: require('./game.js') -> { createState, tick, setInput, ... } */

(function (global) {
  'use strict';

  // ---------------------------------------------------------------- pure logic

  var DIRECTIONS = {
    UP: { x: 0, y: -1 },
    DOWN: { x: 0, y: 1 },
    LEFT: { x: -1, y: 0 },
    RIGHT: { x: 1, y: 0 }
  };

  var OPPOSITE = { UP: 'DOWN', DOWN: 'UP', LEFT: 'RIGHT', RIGHT: 'LEFT' };

  var DIFFICULTIES = {
    slow: { label: 'Slow', tickMs: 200 },
    normal: { label: 'Normal', tickMs: 130 },
    fast: { label: 'Fast', tickMs: 80 }
  };

  var SCORE_PER_FOOD = 10;
  var SPEEDUP_EVERY = 5;          // foods per level-up
  var MAX_LEVEL = 4;              // speed notch cap
  var LEVEL_MULTIPLIERS = [1, 0.8, 0.64, 0.5];
  var MIN_TICK_MS = 40;
  var MAX_SCORES = 5;
  var STORAGE_KEY = 'snake-high-scores-v1';
  var NAME_KEY = 'snake-player-name';
  var DEFAULT_NAME = 'PLAYER';
  var GRID_SIZE = 20;

  /** Fresh running state: length-3 snake heading RIGHT, food on a random empty cell. */
  function createState(size, difficultyKey) {
    var diff = DIFFICULTIES[difficultyKey] || DIFFICULTIES.normal;
    var startX = Math.max(2, Math.floor(size / 2));
    var midY = Math.floor(size / 2);
    var state = {
      size: size,
      difficultyKey: difficultyKey,
      tickMs: diff.tickMs,
      snake: [
        { x: startX, y: midY },
        { x: startX - 1, y: midY },
        { x: startX - 2, y: midY }
      ],
      dir: 'RIGHT',
      pendingDir: null,
      score: 0,
      foodsEaten: 0,
      level: 1,
      status: 'running',   // running | paused | dead | won
      food: null
    };
    state.food = spawnFood(state);
    return state;
  }

  /** Direction that will actually be used this tick: a 180-degree reversal is ignored. */
  function resolveDirection(pending, current) {
    if (!pending) return current;
    if (pending === OPPOSITE[current]) return current;
    return pending;
  }

  /** Queue a direction change. Reversal of the current heading is ignored outright. */
  function setInput(state, dir) {
    if (!DIRECTIONS[dir]) return;
    if (dir === OPPOSITE[state.dir]) return;
    state.pendingDir = dir;
  }

  /** Tick interval for a difficulty at a level, clamped at the fastest notch. */
  function speedForLevel(difficultyKey, level) {
    var diff = DIFFICULTIES[difficultyKey] || DIFFICULTIES.normal;
    var idx = Math.min(Math.max(level, 1), LEVEL_MULTIPLIERS.length) - 1;
    return Math.max(MIN_TICK_MS, Math.round(diff.tickMs * LEVEL_MULTIPLIERS[idx]));
  }

  /** A random empty cell, or null when the board is completely full. */
  function spawnFood(state) {
    var occupied = new Set();
    for (var i = 0; i < state.snake.length; i++) {
      var c = state.snake[i];
      occupied.add(c.x + ',' + c.y);
    }
    if (occupied.size >= state.size * state.size) return null;
    var empty = [];
    for (var y = 0; y < state.size; y++) {
      for (var x = 0; x < state.size; x++) {
        if (!occupied.has(x + ',' + y)) empty.push({ x: x, y: y });
      }
    }
    return empty[Math.floor(Math.random() * empty.length)];
  }

  /** Advance one tick. Mutates and returns the state. */
  function tick(state) {
    if (state.status !== 'running') return state;

    var dir = resolveDirection(state.pendingDir, state.dir);
    state.pendingDir = null;
    state.dir = dir;

    var v = DIRECTIONS[dir];
    var head = state.snake[0];
    var nextHead = { x: head.x + v.x, y: head.y + v.y };

    // Wall -> death.
    if (nextHead.x < 0 || nextHead.y < 0 || nextHead.x >= state.size || nextHead.y >= state.size) {
      state.status = 'dead';
      return state;
    }

    var growing = !!state.food && nextHead.x === state.food.x && nextHead.y === state.food.y;

    // Body collision, checked only against cells that will still exist this tick:
    // when not growing the tail vacates its cell, so it is excluded.
    var limit = growing ? state.snake.length : state.snake.length - 1;
    for (var i = 0; i < limit; i++) {
      var c = state.snake[i];
      if (c.x === nextHead.x && c.y === nextHead.y) {
        state.status = 'dead';
        return state;
      }
    }

    state.snake.unshift(nextHead);
    if (!growing) state.snake.pop();

    if (growing) {
      state.score += SCORE_PER_FOOD;
      state.foodsEaten += 1;
      if (state.foodsEaten % SPEEDUP_EVERY === 0 && state.level < MAX_LEVEL) {
        state.level += 1;
        state.tickMs = speedForLevel(state.difficultyKey, state.level);
      }
      state.food = spawnFood(state);
      if (!state.food) state.status = 'won';   // board full -> win, not a crash
    }

    return state;
  }

  // ---------------------------------------------------------------- high scores

  /** Would `score` earn a row in the top-5 table? */
  function isTopScore(scores, score) {
    if (!(score > 0)) return false;
    if (scores.length < MAX_SCORES) return true;
    return score > scores[scores.length - 1].score;
  }

  /** Insert one entry, sorted by score desc (ties keep insertion order), capped at 5. */
  function insertScore(scores, entry) {
    var name = String(entry.name == null ? '' : entry.name).trim() || DEFAULT_NAME;
    var rec = { name: name.slice(0, 12), score: entry.score };
    var out = scores.concat([rec]);
    out.sort(function (a, b) { return b.score - a.score; });  // stable -> ties keep insertion order
    return out.slice(0, MAX_SCORES);
  }

  /** Read + sanitize the table from a storage-like object ({getItem}). */
  function loadScores(storage) {
    try {
      var raw = storage.getItem(STORAGE_KEY);
      if (!raw) return [];
      var parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      var clean = parsed
        .filter(function (e) { return e && typeof e.score === 'number' && e.score > 0; })
        .map(function (e) {
          var n = String(e.name == null ? '' : e.name).trim() || DEFAULT_NAME;
          return { name: n.slice(0, 12), score: e.score };
        });
      clean.sort(function (a, b) { return b.score - a.score; });
      return clean.slice(0, MAX_SCORES);
    } catch (err) {
      return [];
    }
  }

  /** Persist the table to a storage-like object ({setItem}). */
  function saveScores(storage, scores) {
    try {
      storage.setItem(STORAGE_KEY, JSON.stringify(scores));
      return true;
    } catch (err) {
      return false;
    }
  }

  // ---------------------------------------------------------------- DOM app

  function safeStorage() {
    try {
      var probe = '__snake_probe__';
      global.localStorage.setItem(probe, '1');
      global.localStorage.removeItem(probe);
      return global.localStorage;
    } catch (err) {
      var mem = {};
      return {
        getItem: function (k) { return (k in mem) ? mem[k] : null; },
        setItem: function (k, v) { mem[k] = String(v); },
        removeItem: function (k) { delete mem[k]; }
      };
    }
  }

  function initApp() {
    function $(sel) { return document.querySelector(sel); }

    var els = {
      screens: {
        start: $('#screen-start'),
        game: $('#screen-game'),
        over: $('#screen-over')
      },
      hudScore: $('#hud-score'),
      hudBest: $('#hud-best'),
      hudLevel: $('#hud-level'),
      boardWrap: $('#board-wrap'),
      board: $('#board'),
      pauseOverlay: $('#pause-overlay'),
      startScores: $('#start-scores'),
      overScores: $('#over-scores'),
      overTitle: $('#over-title'),
      overScore: $('#over-score'),
      overQualify: $('#over-qualify'),
      nameInput: $('#name-input'),
      btnSaveScore: $('#btn-save-score'),
      btnPause: $('#btn-pause'),
      btnResume: $('#btn-resume'),
      btnRestart: $('#btn-restart'),
      btnHome: $('#btn-home')
    };

    var ctx = els.board.getContext('2d');
    var storage = safeStorage();
    var CELL = 400 / GRID_SIZE;

    var state = null;
    var timer = null;
    var lastDifficulty = 'normal';
    var touchStart = null;

    function getScores() { return loadScores(storage); }

    function showScreen(name) {
      for (var k in els.screens) {
        els.screens[k].classList.toggle('active', k === name);
      }
      if (name === 'game') fitCanvas();
    }

    function renderScoreList(listEl, scores) {
      listEl.innerHTML = '';
      if (!scores.length) {
        var li = document.createElement('li');
        li.className = 'empty';
        li.textContent = 'No scores yet — be the first!';
        listEl.appendChild(li);
        return;
      }
      scores.forEach(function (entry, i) {
        var li = document.createElement('li');
        var rank = document.createElement('span');
        rank.className = 'rank';
        rank.textContent = (i + 1) + '.';
        var name = document.createElement('span');
        name.className = 'sname';
        name.textContent = entry.name;
        var val = document.createElement('span');
        val.className = 'sval';
        val.textContent = entry.score;
        li.appendChild(rank);
        li.appendChild(name);
        li.appendChild(val);
        listEl.appendChild(li);
      });
    }

    function renderScores() {
      var scores = getScores();
      renderScoreList(els.startScores, scores);
      renderScoreList(els.overScores, scores);
      els.hudBest.textContent = scores.length ? scores[0].score : 0;
      return scores;
    }

    // ----- canvas -----

    function fitCanvas() {
      var avail = Math.min(els.boardWrap.clientWidth || 400, window.innerHeight * 0.45, 520);
      var px = Math.max(260, Math.round(avail));
      els.board.style.width = px + 'px';
      els.board.style.height = px + 'px';
      var dpr = window.devicePixelRatio || 1;
      els.board.width = Math.round(400 * dpr);
      els.board.height = Math.round(400 * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      render();
    }

    function roundRect(x, y, w, h, r) {
      ctx.beginPath();
      ctx.moveTo(x + r, y);
      ctx.arcTo(x + w, y, x + w, y + h, r);
      ctx.arcTo(x + w, y + h, x, y + h, r);
      ctx.arcTo(x, y + h, x, y, r);
      ctx.arcTo(x, y, x + w, y, r);
      ctx.closePath();
      ctx.fill();
    }

    function drawFood() {
      if (!state || !state.food) return;
      var f = state.food;
      var cx = f.x * CELL + CELL / 2;
      var cy = f.y * CELL + CELL / 2;
      var r = CELL * 0.34;
      ctx.fillStyle = 'rgba(239,68,68,0.22)';
      ctx.beginPath();
      ctx.arc(cx, cy, r + 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#ef4444';
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = 'rgba(255,255,255,0.5)';
      ctx.beginPath();
      ctx.arc(cx - r * 0.35, cy - r * 0.35, r * 0.3, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = '#4ade80';
      ctx.lineWidth = 2;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(cx, cy - r + 1);
      ctx.quadraticCurveTo(cx + 1, cy - r - 3, cx + 3, cy - r - 4);
      ctx.stroke();
    }

    function drawSnake() {
      if (!state) return;
      var n = state.snake.length;
      for (var i = n - 1; i >= 0; i--) {
        var c = state.snake[i];
        var pad = 1.5;
        var x = c.x * CELL + pad;
        var y = c.y * CELL + pad;
        var s = CELL - pad * 2;
        if (i === 0) {
          ctx.fillStyle = '#86efac';
          roundRect(x, y, s, s, 5);
          var v = DIRECTIONS[state.dir];
          var ex = x + s / 2 + v.x * (s * 0.22);
          var ey = y + s / 2 + v.y * (s * 0.22);
          var px = -v.y;
          var py = v.x;
          ctx.fillStyle = '#0f172a';
          ctx.beginPath();
          ctx.arc(ex + px * 3.5, ey + py * 3.5, 2.2, 0, Math.PI * 2);
          ctx.fill();
          ctx.beginPath();
          ctx.arc(ex - px * 3.5, ey - py * 3.5, 2.2, 0, Math.PI * 2);
          ctx.fill();
        } else {
          ctx.fillStyle = i < n * 0.5 ? '#22c55e' : '#15803d';
          roundRect(x, y, s, s, 4);
        }
      }
    }

    function render() {
      if (!state) return;
      ctx.clearRect(0, 0, 400, 400);
      ctx.fillStyle = '#0d1526';
      ctx.fillRect(0, 0, 400, 400);
      ctx.fillStyle = 'rgba(255,255,255,0.03)';
      for (var y = 0; y < GRID_SIZE; y++) {
        for (var x = 0; x < GRID_SIZE; x++) {
          if ((x + y) % 2 === 0) ctx.fillRect(x * CELL, y * CELL, CELL, CELL);
        }
      }
      drawFood();
      drawSnake();
      els.hudScore.textContent = state.score;
      els.hudLevel.textContent = state.level;
      var best = getScores();
      els.hudBest.textContent = best.length ? best[0].score : 0;
    }

    // ----- flow -----

    function startTimer() {
      stopTimer();
      timer = setInterval(onTick, state.tickMs);
    }

    function stopTimer() {
      if (timer) { clearInterval(timer); timer = null; }
    }

    function onTick() {
      if (!state || state.status !== 'running') return;
      var prevLevel = state.level;
      tick(state);
      if (state.level !== prevLevel) startTimer();   // speed notch up
      render();
      if (state.status === 'dead' || state.status === 'won') {
        stopTimer();
        showGameOver();
      }
    }

    function startGame(difficultyKey) {
      lastDifficulty = difficultyKey;
      state = createState(GRID_SIZE, difficultyKey);
      els.pauseOverlay.classList.add('hidden');
      showScreen('game');
      render();
      startTimer();
    }

    function pause() {
      if (!state || state.status !== 'running') return;
      state.status = 'paused';
      stopTimer();
      els.pauseOverlay.classList.remove('hidden');
    }

    function resume() {
      if (!state || state.status !== 'paused') return;
      state.status = 'running';
      els.pauseOverlay.classList.add('hidden');
      startTimer();
    }

    function togglePause() {
      if (!state) return;
      if (state.status === 'running') pause();
      else if (state.status === 'paused') resume();
    }

    function input(dir) {
      if (!state || state.status !== 'running') return;
      setInput(state, dir);
    }

    function showGameOver() {
      var won = state.status === 'won';
      els.overTitle.textContent = won ? 'You Win! 🎉' : 'Game Over';
      els.overScore.textContent = state.score;
      var scores = getScores();
      var qualifies = isTopScore(scores, state.score);
      els.overQualify.classList.toggle('hidden', !qualifies);
      if (qualifies) {
        els.nameInput.value = loadName() || DEFAULT_NAME;
      }
      renderScoreList(els.overScores, scores);
      showScreen('over');
    }

    function loadName() {
      try { return String(storage.getItem(NAME_KEY) || ''); } catch (e) { return ''; }
    }

    function saveName(name) {
      try { storage.setItem(NAME_KEY, name); } catch (e) { /* ignore */ }
    }

    function saveScore() {
      if (!state) return;
      var updated = insertScore(getScores(), { name: els.nameInput.value, score: state.score });
      saveScores(storage, updated);
      saveName(els.nameInput.value.trim() || DEFAULT_NAME);
      renderScores();
      els.overQualify.classList.add('hidden');
      els.nameInput.blur();
    }

    function goHome() {
      stopTimer();
      state = null;
      renderScores();
      showScreen('start');
    }

    // ----- events -----

    document.querySelectorAll('.difficulty-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        btn.blur();
        startGame(btn.getAttribute('data-difficulty'));
      });
    });

    document.querySelectorAll('.dpad-btn').forEach(function (btn) {
      btn.addEventListener('pointerdown', function (ev) {
        ev.preventDefault();
        input(btn.getAttribute('data-dir'));
      });
    });

    els.btnPause.addEventListener('click', function () { els.btnPause.blur(); togglePause(); });
    els.btnResume.addEventListener('click', resume);
    els.btnRestart.addEventListener('click', function () { startGame(lastDifficulty); });
    els.btnHome.addEventListener('click', goHome);
    els.btnSaveScore.addEventListener('click', saveScore);

    els.nameInput.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); saveScore(); }
    });

    document.addEventListener('keydown', function (ev) {
      var t = ev.target;
      if (t && t.tagName === 'INPUT') return;
      if (t && t.tagName === 'BUTTON') t.blur();
      var keyMap = {
        ArrowUp: 'UP', ArrowDown: 'DOWN', ArrowLeft: 'LEFT', ArrowRight: 'RIGHT',
        w: 'UP', s: 'DOWN', a: 'LEFT', d: 'RIGHT',
        W: 'UP', S: 'DOWN', A: 'LEFT', D: 'RIGHT'
      };
      var dir = keyMap[ev.key];
      if (dir) { ev.preventDefault(); input(dir); return; }
      if (ev.key === 'p' || ev.key === 'P' || ev.key === ' ') {
        ev.preventDefault();
        togglePause();
      }
    });

    document.addEventListener('visibilitychange', function () {
      if (document.hidden) pause();
    });

    els.boardWrap.addEventListener('touchstart', function (ev) {
      var t = ev.changedTouches[0];
      touchStart = { x: t.clientX, y: t.clientY };
    }, { passive: true });

    els.boardWrap.addEventListener('touchmove', function (ev) {
      ev.preventDefault();
    }, { passive: false });

    els.boardWrap.addEventListener('touchend', function (ev) {
      if (!touchStart) return;
      var t = ev.changedTouches[0];
      var dx = t.clientX - touchStart.x;
      var dy = t.clientY - touchStart.y;
      touchStart = null;
      var ax = Math.abs(dx);
      var ay = Math.abs(dy);
      if (Math.max(ax, ay) < 24) return;
      input(ax > ay ? (dx > 0 ? 'RIGHT' : 'LEFT') : (dy > 0 ? 'DOWN' : 'UP'));
    }, { passive: true });

    window.addEventListener('resize', function () {
      if (state && els.screens.game.classList.contains('active')) fitCanvas();
    });

    // ----- boot -----
    renderScores();
    showScreen('start');
    fitCanvas();
  }

  if (typeof document !== 'undefined' && document.getElementById('app')) {
    initApp();
  }

  // ---------------------------------------------------------------- exports

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      DIRECTIONS: DIRECTIONS,
      OPPOSITE: OPPOSITE,
      DIFFICULTIES: DIFFICULTIES,
      SCORE_PER_FOOD: SCORE_PER_FOOD,
      SPEEDUP_EVERY: SPEEDUP_EVERY,
      MAX_LEVEL: MAX_LEVEL,
      MIN_TICK_MS: MIN_TICK_MS,
      MAX_SCORES: MAX_SCORES,
      STORAGE_KEY: STORAGE_KEY,
      DEFAULT_NAME: DEFAULT_NAME,
      GRID_SIZE: GRID_SIZE,
      createState: createState,
      resolveDirection: resolveDirection,
      setInput: setInput,
      tick: tick,
      spawnFood: spawnFood,
      speedForLevel: speedForLevel,
      isTopScore: isTopScore,
      insertScore: insertScore,
      loadScores: loadScores,
      saveScores: saveScores
    };
  }
})(typeof window !== 'undefined' ? window : globalThis);
