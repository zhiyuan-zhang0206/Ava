#!/usr/bin/env node
'use strict';

/* Snake core-logic tests — plain Node, zero dependencies: node test.js */

var G = require('./game.js');

var passed = 0;
var failed = 0;
var failures = [];

function assert(cond, msg) {
  if (cond) { passed++; }
  else { failed++; failures.push(msg || 'assertion failed'); }
}

function deepEq(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    return a.every(function (v, i) { return deepEq(v, b[i]); });
  }
  if (a && b && typeof a === 'object') {
    var ka = Object.keys(a);
    var kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    return ka.every(function (k) { return deepEq(a[k], b[k]); });
  }
  return false;
}

function eq(actual, expected, msg) {
  if (deepEq(actual, expected)) { passed++; }
  else {
    failed++;
    failures.push((msg || 'not equal') + ' — got ' + JSON.stringify(actual) + ', want ' + JSON.stringify(expected));
  }
}

function section(name) { console.log('— ' + name); }

function placeFoodAhead(s) {
  var v = G.DIRECTIONS[s.dir];
  var h = s.snake[0];
  s.food = { x: h.x + v.x, y: h.y + v.y };
}

function eatOne(s) {
  var v = G.DIRECTIONS[s.dir];
  var h = s.snake[0];
  var nx = h.x + v.x;
  var ny = h.y + v.y;
  if (nx < 0 || ny < 0 || nx >= s.size || ny >= s.size) s.dir = 'DOWN';
  placeFoodAhead(s);
  G.tick(s);
}

// ---------------------------------------------------------------- movement

section('movement');
{
  var s = G.createState(20, 'normal');
  var head = s.snake[0];
  eq(s.snake.length, 3, 'starts with length 3');
  eq(s.dir, 'RIGHT', 'starts moving right');
  s.food = { x: 15, y: 15 };
  G.tick(s);
  eq(s.snake[0], { x: head.x + 1, y: head.y }, 'moves one cell per tick');
  eq(s.snake.length, 3, 'length unchanged without food');
  eq(s.status, 'running', 'still running');

  G.setInput(s, 'UP');
  G.tick(s);
  eq(s.snake[0], { x: head.x + 1, y: head.y - 1 }, 'turns on input');
}

// ---------------------------------------------------------------- reversal

section('reversal input ignored');
{
  var s = G.createState(20, 'normal');
  s.snake = [{ x: 5, y: 5 }, { x: 4, y: 5 }, { x: 3, y: 5 }];
  s.dir = 'RIGHT';
  s.food = { x: 15, y: 15 };
  G.setInput(s, 'LEFT');
  eq(s.pendingDir, null, 'reversal is not queued');
  G.tick(s);
  eq(s.snake[0], { x: 6, y: 5 }, 'keeps moving right after reversal input');
  eq(s.status, 'running', 'reversal does not kill');

  eq(G.resolveDirection('DOWN', 'UP'), 'UP', 'resolveDirection ignores reversal');
  eq(G.resolveDirection('LEFT', 'RIGHT'), 'RIGHT', 'resolveDirection ignores reversal (2)');
  eq(G.resolveDirection('RIGHT', 'RIGHT'), 'RIGHT', 'same direction kept');
  eq(G.resolveDirection('UP', 'LEFT'), 'UP', 'perpendicular turn applied');
  eq(G.resolveDirection(null, 'LEFT'), 'LEFT', 'no input keeps heading');

  var s2 = G.createState(20, 'normal');
  s2.dir = 'DOWN';
  G.setInput(s2, 'BOGUS');
  eq(s2.pendingDir, null, 'invalid input ignored');
}

// ---------------------------------------------------------------- growth

section('growth (tail does not move on the growth tick)');
{
  var s = G.createState(20, 'normal');
  s.snake = [{ x: 5, y: 5 }, { x: 4, y: 5 }, { x: 3, y: 5 }];
  s.dir = 'RIGHT';
  s.food = { x: 6, y: 5 };
  var tailBefore = s.snake[s.snake.length - 1];
  G.tick(s);
  eq(s.snake.length, 4, 'grows by one');
  eq(s.snake[0], { x: 6, y: 5 }, 'head moved onto food');
  eq(s.snake[3], tailBefore, 'old tail cell did not move');
  eq(s.score, 10, 'score +10');
  eq(s.foodsEaten, 1, 'foodsEaten incremented');
  eq(s.status, 'running', 'still running');
  assert(s.food !== null, 'new food spawned immediately');
  assert(!(s.food.x === 6 && s.food.y === 5), 'new food is not on the eaten cell');
  var onSnake = s.snake.some(function (c) { return c.x === s.food.x && c.y === s.food.y; });
  assert(!onSnake, 'new food is not on the snake');
}

// ---------------------------------------------------------------- tail vacate

section('tail-vacate move is legal');
{
  var s = G.createState(20, 'normal');
  s.snake = [
    { x: 1, y: 2 }, { x: 0, y: 2 }, { x: 0, y: 1 }, { x: 1, y: 1 }, { x: 2, y: 1 }, { x: 2, y: 2 }
  ];
  s.dir = 'RIGHT';
  s.food = { x: 10, y: 10 };
  G.tick(s);
  eq(s.status, 'running', 'moving into the tail cell is legal when not growing');
  eq(s.snake.length, 6, 'length unchanged');
  eq(s.snake[0], { x: 2, y: 2 }, 'head now occupies the old tail cell');
  var seen = {};
  s.snake.forEach(function (c) { seen[c.x + ',' + c.y] = true; });
  eq(Object.keys(seen).length, s.snake.length, 'no overlapping cells after tail-vacate move');
}

// ---------------------------------------------------------------- collisions

section('self-collision is death');
{
  var s = G.createState(20, 'normal');
  s.snake = [
    { x: 1, y: 2 }, { x: 0, y: 2 }, { x: 0, y: 1 }, { x: 1, y: 1 }, { x: 2, y: 1 }, { x: 2, y: 2 }
  ];
  s.dir = 'UP';
  s.food = { x: 10, y: 10 };
  G.tick(s);
  eq(s.status, 'dead', 'hitting own body kills');
  var len = s.snake.length;
  G.tick(s);
  eq(s.snake.length, len, 'no further movement after death');
}

section('wall collision is death');
{
  var s = G.createState(20, 'normal');
  s.snake = [{ x: 19, y: 10 }, { x: 18, y: 10 }, { x: 17, y: 10 }];
  s.dir = 'RIGHT';
  s.food = { x: 5, y: 5 };
  G.tick(s);
  eq(s.status, 'dead', 'hitting the wall kills');
}

// ---------------------------------------------------------------- food spawn

section('food spawns on empty cells only');
{
  var s = G.createState(20, 'normal');
  assert(s.food !== null, 'initial food spawned');
  var onSnake = s.snake.some(function (c) { return c.x === s.food.x && c.y === s.food.y; });
  assert(!onSnake, 'initial food not on snake');

  for (var i = 0; i < 500; i++) {
    var f = G.spawnFood(s);
    assert(f !== null, 'food exists while cells are free');
    var bad = s.snake.some(function (c) { return c.x === f.x && c.y === f.y; });
    assert(!bad, 'spawnFood never lands on the snake');
  }

  var s2 = G.createState(20, 'normal');
  var all = [];
  for (var y = 0; y < 20; y++) {
    for (var x = 0; x < 20; x++) all.push({ x: x, y: y });
  }
  s2.snake = all.slice(0, 399);
  var only = G.spawnFood(s2);
  eq(only, all[399], 'only free cell is chosen');
  s2.snake = all;
  eq(G.spawnFood(s2), null, 'full board -> no food');
}

// ---------------------------------------------------------------- win

section('full-board win');
{
  var s = G.createState(3, 'normal');
  s.snake = [
    { x: 1, y: 0 }, { x: 2, y: 0 }, { x: 2, y: 1 }, { x: 2, y: 2 },
    { x: 1, y: 2 }, { x: 0, y: 2 }, { x: 0, y: 1 }, { x: 1, y: 1 }
  ];
  s.dir = 'LEFT';
  s.food = { x: 0, y: 0 };
  s.pendingDir = null;
  s.status = 'running';
  G.tick(s);
  eq(s.status, 'won', 'filling the last cell is a win, not a crash');
  eq(s.snake.length, 9, 'snake fills the whole board');
  eq(s.score, 10, 'win still scores');
  eq(s.food, null, 'no food left on a full board');
}

// ---------------------------------------------------------------- scoring

section('score accumulation');
{
  var s = G.createState(20, 'normal');
  s.snake = [{ x: 5, y: 5 }, { x: 4, y: 5 }, { x: 3, y: 5 }];
  s.dir = 'RIGHT';
  s.food = { x: 6, y: 5 };
  G.tick(s);
  s.food = { x: 7, y: 5 };
  G.tick(s);
  eq(s.score, 20, 'two foods -> 20 points');
  eq(s.snake.length, 5, 'two foods -> length 5');
}

// ---------------------------------------------------------------- speed-up

section('speed-up every 5 foods, capped');
{
  var s = G.createState(20, 'normal');
  s.snake = [{ x: 5, y: 5 }, { x: 4, y: 5 }, { x: 3, y: 5 }];
  s.dir = 'RIGHT';
  s.food = { x: 6, y: 5 };
  var base = s.tickMs;
  eq(base, G.DIFFICULTIES.normal.tickMs, 'starts at the difficulty tick rate');

  for (var i = 0; i < 5; i++) eatOne(s);
  eq(s.level, 2, 'level 2 after 5 foods');
  eq(s.tickMs, G.speedForLevel('normal', 2), 'tick faster at level 2');
  assert(s.tickMs < base, 'speed increased');

  for (var j = 0; j < 5; j++) eatOne(s);
  eq(s.level, 3, 'level 3 after 10 foods');
  eq(s.tickMs, G.speedForLevel('normal', 3), 'tick faster at level 3');

  for (var k = 0; k < 5; k++) eatOne(s);
  eq(s.level, 4, 'level 4 after 15 foods');
  eq(s.tickMs, G.speedForLevel('normal', 4), 'tick faster at level 4');

  var capped = s.tickMs;
  for (var m = 0; m < 5; m++) eatOne(s);
  eq(s.level, 4, 'level stays 4 after 20 foods');
  eq(s.tickMs, capped, 'speed capped at the fastest notch');
  eq(s.score, 200, '20 foods -> 200 points');
}

// ---------------------------------------------------------------- difficulties

section('difficulty tick speeds');
{
  eq(G.DIFFICULTIES.slow.tickMs, 200, 'slow = 200ms');
  eq(G.DIFFICULTIES.normal.tickMs, 130, 'normal = 130ms');
  eq(G.DIFFICULTIES.fast.tickMs, 80, 'fast = 80ms');
  assert(G.DIFFICULTIES.slow.tickMs > G.DIFFICULTIES.normal.tickMs, 'slow slower than normal');
  assert(G.DIFFICULTIES.normal.tickMs > G.DIFFICULTIES.fast.tickMs, 'normal slower than fast');
  eq(G.createState(20, 'slow').tickMs, 200, 'state uses chosen difficulty');
  eq(G.createState(20, 'fast').tickMs, 80, 'state uses chosen difficulty (fast)');
  eq(G.createState(20, 'bogus').tickMs, 130, 'unknown difficulty falls back to normal');
  eq(G.speedForLevel('fast', 4), 40, 'fastest notch hits the floor');
  eq(G.speedForLevel('fast', 9), G.speedForLevel('fast', 4), 'level beyond cap clamps');
  assert(G.speedForLevel('slow', 2) < G.speedForLevel('slow', 1), 'level 2 faster than level 1');
}

// ---------------------------------------------------------------- paused

section('paused state does not advance');
{
  var s = G.createState(20, 'normal');
  s.status = 'paused';
  var len = s.snake.length;
  var score = s.score;
  G.tick(s);
  eq(s.snake.length, len, 'no movement while paused');
  eq(s.score, score, 'no scoring while paused');
  eq(s.status, 'paused', 'stays paused');
}

// ---------------------------------------------------------------- high scores

section('high-score table: insert order, cap, qualification');
{
  var t = G.insertScore([], { name: 'A', score: 30 });
  eq(t, [{ name: 'A', score: 30 }], 'first entry');

  t = G.insertScore(t, { name: 'B', score: 50 });
  eq(t, [{ name: 'B', score: 50 }, { name: 'A', score: 30 }], 'new best goes first');

  t = G.insertScore(t, { name: 'C', score: 10 });
  eq(t, [{ name: 'B', score: 50 }, { name: 'A', score: 30 }, { name: 'C', score: 10 }], 'lowest goes last');

  t = G.insertScore(t, { name: 'D', score: 40 });
  eq(t, [
    { name: 'B', score: 50 }, { name: 'D', score: 40 },
    { name: 'A', score: 30 }, { name: 'C', score: 10 }
  ], 'inserts in rank order');

  t = G.insertScore(t, { name: 'E', score: 30 });
  eq(t, [
    { name: 'B', score: 50 }, { name: 'D', score: 40 },
    { name: 'A', score: 30 }, { name: 'E', score: 30 }, { name: 'C', score: 10 }
  ], 'tie keeps insertion order (after the older equal)');

  var before = t.slice();
  t = G.insertScore(t, { name: 'F', score: 5 });
  eq(t, before, 'non-top-5 score does not enter the table');

  eq(t.length, 5, 'table capped at 5 entries');

  eq(G.isTopScore([], 1), true, 'first score qualifies');
  eq(G.isTopScore([], 0), false, 'zero never qualifies');
  eq(G.isTopScore([{ score: 100 }, { score: 90 }, { score: 80 }, { score: 70 }], 1), true, 'room left -> qualifies');
  var full = [{ score: 100 }, { score: 90 }, { score: 80 }, { score: 70 }, { score: 60 }];
  eq(G.isTopScore(full, 60), false, 'tie with the last slot does not qualify when full');
  eq(G.isTopScore(full, 65), true, 'beats the last slot -> qualifies');

  var named = G.insertScore([], { name: '   ', score: 10 });
  eq(named[0].name, G.DEFAULT_NAME, 'blank name defaults');
  var long = G.insertScore([], { name: 'ABCDEFGHIJKLMNOP', score: 10 });
  eq(long[0].name, 'ABCDEFGHIJKL', 'name truncated to 12 chars');
}

section('high-score persistence round-trip');
{
  function fakeStorage(seed) {
    var data = Object.assign({}, seed);
    return {
      getItem: function (k) { return (k in data) ? data[k] : null; },
      setItem: function (k, v) { data[k] = String(v); },
      removeItem: function (k) { delete data[k]; }
    };
  }

  var st = fakeStorage();
  var saved = G.saveScores(st, [{ name: 'A', score: 10 }, { name: 'B', score: 20 }]);
  eq(saved, true, 'save succeeds');
  eq(G.loadScores(st), [{ name: 'B', score: 20 }, { name: 'A', score: 10 }], 'round-trip preserves entries (sorted desc)');

  var st2 = fakeStorage({});
  st2.setItem(G.STORAGE_KEY, 'not json{');
  eq(G.loadScores(st2), [], 'corrupt data -> empty table');

  var st3 = fakeStorage({});
  st3.setItem(G.STORAGE_KEY, '{"a":1}');
  eq(G.loadScores(st3), [], 'wrong shape -> empty table');

  var st4 = fakeStorage({});
  st4.setItem(G.STORAGE_KEY, JSON.stringify([
    { name: 'X', score: -5 },
    { name: 'Y', score: 7 },
    { name: 'Z', score: 'nope' }
  ]));
  eq(G.loadScores(st4), [{ name: 'Y', score: 7 }], 'invalid entries filtered');

  // reload = a fresh storage seeded with what was saved
  var st5 = fakeStorage({});
  G.saveScores(st5, G.insertScore([], { name: 'P', score: 99 }));
  var st6 = fakeStorage({});
  st6.setItem(G.STORAGE_KEY, st5.getItem(G.STORAGE_KEY));
  eq(G.loadScores(st6), [{ name: 'P', score: 99 }], 'reload sees the saved table');
}

// ---------------------------------------------------------------- summary

console.log('');
console.log('✔ ' + passed + ' assertions passed, ✘ ' + failed + ' failed');
if (failed > 0) {
  failures.forEach(function (f) { console.log('  ✗ ' + f); });
  process.exit(1);
}
console.log('ALL TESTS PASSED');
