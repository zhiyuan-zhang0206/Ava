"use strict";

/* 2048 core-logic tests — plain Node, no dependencies.
 * Run with: node test.js
 */

const {
  SIZE,
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
} = require("./game.js");

let passed = 0;
let failed = 0;

function fail(msg) {
  throw new Error(msg);
}

function assertEq(actual, expected, msg) {
  if (actual !== expected) {
    fail((msg || "assertEq") + ": expected " + expected + ", got " + actual);
  }
}

function assertTrue(v, msg) {
  if (!v) fail(msg || "expected truthy, got falsy");
}

function assertFalse(v, msg) {
  if (v) fail(msg || "expected falsy, got truthy");
}

function assertBoard(actual, expected, msg) {
  if (actual.join(",") !== expected.join(",")) {
    fail(
      (msg || "board mismatch") +
        "\n  expected [" + expected.join(", ") + "]\n  got      [" + actual.join(", ") + "]"
    );
  }
}

function boardFrom(rows) {
  const b = createBoard();
  for (let r = 0; r < SIZE; r++) {
    for (let c = 0; c < SIZE; c++) {
      b[r * SIZE + c] = rows[r][c];
    }
  }
  return b;
}

// Deterministic RNG: returns the given values in order, then repeats the last.
function seqRng() {
  const vals = Array.prototype.slice.call(arguments);
  let i = 0;
  return function () {
    const v = vals[Math.min(i, vals.length - 1)];
    i++;
    return v;
  };
}

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log("  ok   " + name);
  } catch (e) {
    failed++;
    console.log("  FAIL " + name);
    console.log("       " + String(e.message).split("\n").join("\n       "));
  }
}

console.log("2048 core logic tests");
console.log("");

/* --- slide / merge math --- */

test("slideLine: [2,2,0,0] -> [4,0,0,0], gained 4", function () {
  const r = slideLine([2, 2, 0, 0]);
  assertBoard(r.line, [4, 0, 0, 0]);
  assertEq(r.gained, 4);
});

test("slideLine: [2,2,4,4] -> [4,8,0,0], gained 12", function () {
  const r = slideLine([2, 2, 4, 4]);
  assertBoard(r.line, [4, 8, 0, 0]);
  assertEq(r.gained, 12);
});

test("slideLine: no double merge ([4,4,4,4] -> [8,8,0,0])", function () {
  const r = slideLine([4, 4, 4, 4]);
  assertBoard(r.line, [8, 8, 0, 0]);
  assertEq(r.gained, 16);
  assertBoard(slideLine([2, 2, 2, 0]).line, [4, 2, 0, 0]);
  assertBoard(slideLine([2, 2, 2, 2]).line, [4, 4, 0, 0], "[2,2,2,2] -> [4,4,0,0]: fresh 4s do not re-merge");
  assertEq(slideLine([2, 2, 2, 2]).gained, 8);
  assertBoard(slideLine([2, 0, 2, 2]).line, [4, 2, 0, 0]);
});

test("slideLine: already-packed line is a no-op", function () {
  const r = slideLine([2, 4, 8, 16]);
  assertBoard(r.line, [2, 4, 8, 16]);
  assertEq(r.gained, 0);
});

/* --- slide in all four directions --- */

test("slide LEFT merges and packs", function () {
  const b = boardFrom([[0, 2, 0, 2], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]);
  const r = slide(b, DIR.LEFT);
  assertTrue(r.moved);
  assertEq(r.score, 4);
  assertBoard(r.board, [4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
});

test("slide RIGHT merges and packs", function () {
  const b = boardFrom([[0, 2, 0, 2], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]);
  const r = slide(b, DIR.RIGHT);
  assertTrue(r.moved);
  assertEq(r.score, 4);
  assertBoard(r.board, [0, 0, 0, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
});

test("slide UP merges and packs", function () {
  const b = boardFrom([[2, 0, 0, 0], [2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]);
  const r = slide(b, DIR.UP);
  assertTrue(r.moved);
  assertEq(r.score, 4);
  assertBoard(r.board, [4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
});

test("slide DOWN merges and packs", function () {
  const b = boardFrom([[2, 0, 0, 0], [2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]);
  const r = slide(b, DIR.DOWN);
  assertTrue(r.moved);
  assertEq(r.score, 4);
  assertBoard(r.board, [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4, 0, 0, 0]);
});

test("slide: score accumulates across rows", function () {
  const b = boardFrom([[2, 2, 2, 2], [2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]);
  const r = slide(b, DIR.LEFT);
  assertEq(r.score, 12);
  assertBoard(r.board, [4, 4, 0, 0, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
});

test("slide: no-op slide reports moved=false and score 0", function () {
  const b = boardFrom([[2, 4, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]);
  const r = slide(b, DIR.LEFT);
  assertFalse(r.moved);
  assertEq(r.score, 0);
  assertBoard(r.board, b);
});

test("slide: does not mutate the input board", function () {
  const b = boardFrom([[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]);
  const before = b.slice();
  slide(b, DIR.LEFT);
  assertBoard(b, before);
});

test("locked full board: no movement in any direction", function () {
  const rows = [[2, 4, 2, 4], [4, 2, 4, 2], [2, 4, 2, 4], [4, 2, 4, 2]];
  const dirs = [DIR.LEFT, DIR.UP, DIR.RIGHT, DIR.DOWN];
  for (let d = 0; d < dirs.length; d++) {
    const b = boardFrom(rows);
    const r = slide(b, dirs[d]);
    assertFalse(r.moved, "direction " + d);
    assertEq(r.score, 0, "direction " + d);
    assertBoard(r.board, b, "direction " + d);
  }
});

/* --- random spawn --- */

test("addRandomTile: spawns 2 in a random empty cell (90% roll)", function () {
  const b = createBoard();
  addRandomTile(b, seqRng(0, 0.5));
  assertEq(b[0], 2);
  assertEq(emptyCells(b).length, SIZE * SIZE - 1);
});

test("addRandomTile: spawns 4 on the 10% roll", function () {
  const b = createBoard();
  addRandomTile(b, seqRng(0, 0.95));
  assertEq(b[0], 4);
});

test("addRandomTile: returns false on a full board", function () {
  const b = boardFrom([[2, 4, 2, 4], [4, 2, 4, 2], [2, 4, 2, 4], [4, 2, 4, 2]]);
  assertFalse(addRandomTile(b, seqRng(0, 0.5)));
});

test("move: one tile spawns after a slide", function () {
  const b = boardFrom([[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]);
  const r = move(b, DIR.LEFT, seqRng(0.5, 0.5));
  assertTrue(r.moved);
  assertEq(r.score, 4);
  assertEq(r.board[0], 4, "merge result");
  assertEq(r.board[8], 2, "spawned tile (7th of 15 empty cells, flat index 8)");
  assertEq(emptyCells(r.board).length, 14, "exactly one tile spawned");
});

test("move: spawned tile is 4 when the 10% roll hits", function () {
  const b = boardFrom([[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]);
  const r = move(b, DIR.LEFT, seqRng(0.5, 0.95));
  assertEq(r.board[8], 4);
});

test("move: no spawn when nothing moved", function () {
  const b = boardFrom([[2, 4, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]);
  const r = move(b, DIR.LEFT, seqRng(0.5, 0.5));
  assertFalse(r.moved);
  assertBoard(r.board, b);
});

test("move: no spawn on a full locked board", function () {
  const b = boardFrom([[2, 4, 2, 4], [4, 2, 4, 2], [2, 4, 2, 4], [4, 2, 4, 2]]);
  const r = move(b, DIR.LEFT, seqRng(0.5, 0.5));
  assertFalse(r.moved);
  assertBoard(r.board, b);
});

/* --- win / lose --- */

test("hasWon: true when a 2048 (or higher) tile exists", function () {
  const b = createBoard();
  b[0] = 2048;
  assertTrue(hasWon(b));
  b[0] = 4096;
  assertTrue(hasWon(b));
  b[0] = 1024;
  assertFalse(hasWon(b));
});

test("hasWon: a merge can create the winning tile", function () {
  const b = boardFrom([[1024, 1024, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]);
  const r = move(b, DIR.LEFT, seqRng(0.5, 0.5));
  assertTrue(hasWon(r.board));
});

test("isGameOver: true on a full locked board", function () {
  const b = boardFrom([[2, 4, 2, 4], [4, 2, 4, 2], [2, 4, 2, 4], [4, 2, 4, 2]]);
  assertTrue(isFull(b));
  assertFalse(hasMoves(b));
  assertTrue(isGameOver(b));
});

test("isGameOver: false when a full board still has a merge", function () {
  const b = boardFrom([[2, 2, 4, 8], [4, 8, 2, 4], [2, 4, 8, 16], [16, 8, 4, 2]]);
  assertTrue(isFull(b));
  assertTrue(hasMoves(b));
  assertFalse(isGameOver(b));
});

test("isGameOver: false while any empty cell exists", function () {
  const b = boardFrom([[2, 4, 2, 4], [4, 2, 4, 2], [2, 4, 2, 4], [4, 2, 4, 0]]);
  assertFalse(isFull(b));
  assertTrue(hasMoves(b));
  assertFalse(isGameOver(b));
});

/* --- game setup / restart --- */

test("newGame: starts with exactly two tiles of 2 or 4", function () {
  const b = newGame(seqRng(0, 0.5, 0, 0.5));
  const tiles = b.filter(function (v) { return v !== 0; });
  assertEq(tiles.length, 2);
  assertTrue(tiles.every(function (v) { return v === 2 || v === 4; }));
  assertFalse(hasWon(b));
});

test("restart reset: resetState clears board, score and flags", function () {
  const state = createState(seqRng(0, 0.5, 0, 0.5));
  state.board = boardFrom([[2, 2, 4, 8], [4, 8, 2, 4], [2, 4, 8, 16], [16, 8, 4, 2]]);
  state.score = 12345;
  state.won = true;
  state.keepGoing = true;
  state.over = true;
  resetState(state, seqRng(0, 0.5, 0, 0.5));
  assertEq(state.score, 0);
  assertFalse(state.won);
  assertFalse(state.keepGoing);
  assertFalse(state.over);
  const tiles = state.board.filter(function (v) { return v !== 0; });
  assertEq(tiles.length, 2);
  assertTrue(tiles.every(function (v) { return v === 2 || v === 4; }));
});

/* --- summary --- */

console.log("");
console.log(passed + " passed, " + failed + " failed");
if (failed > 0) {
  process.exit(1);
}
