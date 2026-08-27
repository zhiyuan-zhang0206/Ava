"use strict";

/* 2048 — core logic + browser UI in one file, zero dependencies.
 * The pure logic is exported for Node via the guard at the bottom;
 * the UI only initializes when `window` and `document` exist (browser).
 */

const SIZE = 4;
const WIN_VALUE = 2048;
const SPAWN_2_PROBABILITY = 0.9;

const DIR = Object.freeze({ LEFT: 0, UP: 1, RIGHT: 2, DOWN: 3 });

/* ---------- board helpers ---------- */

function createBoard() {
  return new Array(SIZE * SIZE).fill(0);
}

function idx(r, c) {
  return r * SIZE + c;
}

function emptyCells(board) {
  const cells = [];
  for (let i = 0; i < board.length; i++) {
    if (board[i] === 0) cells.push(i);
  }
  return cells;
}

function isFull(board) {
  return emptyCells(board).length === 0;
}

/* ---------- spawning ---------- */

function addRandomTile(board, rng) {
  const rand = rng || Math.random;
  const cells = emptyCells(board);
  if (cells.length === 0) return false;
  const cell = cells[Math.floor(rand() * cells.length)];
  board[cell] = rand() < SPAWN_2_PROBABILITY ? 2 : 4;
  return true;
}

function newGame(rng) {
  const board = createBoard();
  addRandomTile(board, rng);
  addRandomTile(board, rng);
  return board;
}

/* ---------- slide / merge ---------- */

// Slide one line of SIZE values toward index 0; each tile merges at most once.
function slideLine(line) {
  const tiles = line.filter((v) => v !== 0);
  const out = [];
  let gained = 0;
  for (let i = 0; i < tiles.length; i++) {
    if (i + 1 < tiles.length && tiles[i] === tiles[i + 1]) {
      out.push(tiles[i] * 2);
      gained += tiles[i] * 2;
      i++; // skip the merged partner — no double merge per move
    } else {
      out.push(tiles[i]);
    }
  }
  while (out.length < SIZE) out.push(0);
  return { line: out, gained };
}

// (dRow, dCol) per direction: LEFT, UP, RIGHT, DOWN.
const DIR_OFFSETS = [
  [0, 1],
  [1, 0],
  [0, -1],
  [-1, 0],
];

function slide(board, dir) {
  const [dRow, dCol] = DIR_OFFSETS[dir];
  const horizontal = dir === DIR.LEFT || dir === DIR.RIGHT;
  const out = board.slice();
  let score = 0;

  for (let i = 0; i < SIZE; i++) {
    const r = horizontal ? i : dir === DIR.UP ? 0 : SIZE - 1;
    const c = horizontal ? (dir === DIR.LEFT ? 0 : SIZE - 1) : i;
    const line = [];
    for (let k = 0; k < SIZE; k++) {
      line.push(out[idx(r + dRow * k, c + dCol * k)]);
    }
    const res = slideLine(line);
    score += res.gained;
    for (let k = 0; k < SIZE; k++) {
      out[idx(r + dRow * k, c + dCol * k)] = res.line[k];
    }
  }

  const moved = out.some((v, i) => v !== board[i]);
  return { board: out, score, moved };
}

// A full move: slide, then spawn one tile iff the board changed.
function move(board, dir, rng) {
  const res = slide(board, dir);
  if (res.moved) addRandomTile(res.board, rng);
  return res;
}

/* ---------- win / lose ---------- */

function hasWon(board) {
  return board.some((v) => v >= WIN_VALUE);
}

function hasMoves(board) {
  for (let r = 0; r < SIZE; r++) {
    for (let c = 0; c < SIZE; c++) {
      const v = board[idx(r, c)];
      if (v === 0) return true;
      if (c + 1 < SIZE && board[idx(r, c + 1)] === v) return true;
      if (r + 1 < SIZE && board[idx(r + 1, c)] === v) return true;
    }
  }
  return false;
}

function isGameOver(board) {
  return isFull(board) && !hasMoves(board);
}

/* ---------- game state ---------- */

function createState(rng) {
  return {
    board: newGame(rng),
    score: 0,
    won: false,
    keepGoing: false,
    over: false,
  };
}

function resetState(state, rng) {
  state.board = newGame(rng);
  state.score = 0;
  state.won = false;
  state.keepGoing = false;
  state.over = false;
  return state;
}

/* ---------- browser UI ---------- */

if (typeof window !== "undefined" && typeof document !== "undefined") {
  const gridEl = document.getElementById("board");
  const scoreEl = document.getElementById("score");
  const bestEl = document.getElementById("best");
  const overlayEl = document.getElementById("overlay");
  const overlayTitleEl = document.getElementById("overlay-title");
  const overlayActionsEl = document.getElementById("overlay-actions");
  const restartBtn = document.getElementById("restart");

  const cells = [];
  for (let i = 0; i < SIZE * SIZE; i++) {
    const cell = document.createElement("div");
    cell.className = "cell";
    gridEl.appendChild(cell);
    cells.push(cell);
  }

  let state = createState();
  let best = loadBest();
  let overlayShown = false;

  function loadBest() {
    try {
      return parseInt(window.localStorage.getItem("2048-best") || "0", 10) || 0;
    } catch (e) {
      return 0;
    }
  }

  function saveBest() {
    try {
      window.localStorage.setItem("2048-best", String(best));
    } catch (e) {
      /* storage unavailable — best is session-only */
    }
  }

  function render() {
    for (let i = 0; i < cells.length; i++) {
      const v = state.board[i];
      const cell = cells[i];
      if (v === 0) {
        cell.className = "cell";
        cell.textContent = "";
      } else {
        const cls = v <= WIN_VALUE ? "cell tile tile-" + v : "cell tile tile-super";
        cell.className = cls;
        if (cell.textContent !== String(v)) {
          cell.textContent = String(v);
          cell.classList.remove("pop");
          void cell.offsetWidth; // reflow so the pop animation restarts
          cell.classList.add("pop");
        }
      }
    }
    scoreEl.textContent = String(state.score);
    bestEl.textContent = String(best);
  }

  function showOverlay(title) {
    overlayTitleEl.textContent = title;
    overlayEl.classList.remove("hidden");
    overlayShown = true;
  }

  function hideOverlay() {
    overlayEl.classList.add("hidden");
    overlayShown = false;
  }

  function mkButton(label, onClick) {
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.textContent = label;
    btn.addEventListener("click", onClick);
    return btn;
  }

  function showWin() {
    overlayActionsEl.innerHTML = "";
    overlayActionsEl.appendChild(
      mkButton("Keep going", () => {
        state.keepGoing = true;
        hideOverlay();
        if (isGameOver(state.board)) showGameOver();
      })
    );
    overlayActionsEl.appendChild(mkButton("Restart", restart));
    showOverlay("🎉 You won!");
  }

  function showGameOver() {
    state.over = true;
    overlayActionsEl.innerHTML = "";
    overlayActionsEl.appendChild(mkButton("Restart", restart));
    showOverlay("Game over");
  }

  function restart() {
    resetState(state);
    hideOverlay();
    render();
  }

  function handleMove(dir) {
    if (overlayShown || state.over) return;
    const res = move(state.board, dir);
    if (!res.moved) return;
    state.board = res.board;
    state.score += res.score;
    if (state.score > best) {
      best = state.score;
      saveBest();
    }
    render();
    if (!state.won && hasWon(state.board)) {
      state.won = true;
      showWin();
    } else if (isGameOver(state.board)) {
      showGameOver();
    }
  }

  const KEYMAP = {
    ArrowLeft: DIR.LEFT, ArrowUp: DIR.UP, ArrowRight: DIR.RIGHT, ArrowDown: DIR.DOWN,
    a: DIR.LEFT, w: DIR.UP, d: DIR.RIGHT, s: DIR.DOWN,
    A: DIR.LEFT, W: DIR.UP, D: DIR.RIGHT, S: DIR.DOWN,
  };

  window.addEventListener("keydown", (e) => {
    const dir = KEYMAP[e.key];
    if (dir !== undefined) {
      e.preventDefault();
      handleMove(dir);
    }
  });

  let touchStart = null;
  gridEl.addEventListener(
    "touchstart",
    (e) => {
      touchStart = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    },
    { passive: true }
  );
  gridEl.addEventListener(
    "touchend",
    (e) => {
      if (!touchStart) return;
      const dx = e.changedTouches[0].clientX - touchStart.x;
      const dy = e.changedTouches[0].clientY - touchStart.y;
      touchStart = null;
      if (Math.max(Math.abs(dx), Math.abs(dy)) < 24) return;
      const dir =
        Math.abs(dx) > Math.abs(dy)
          ? dx > 0 ? DIR.RIGHT : DIR.LEFT
          : dy > 0 ? DIR.DOWN : DIR.UP;
      handleMove(dir);
    },
    { passive: true }
  );

  restartBtn.addEventListener("click", restart);

  render();
}

/* ---------- Node export guard ---------- */

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    SIZE,
    WIN_VALUE,
    DIR,
    createBoard,
    emptyCells,
    isFull,
    addRandomTile,
    newGame,
    slideLine,
    slide,
    move,
    hasWon,
    hasMoves,
    isGameOver,
    createState,
    resetState,
  };
}
