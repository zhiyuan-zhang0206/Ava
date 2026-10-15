import test from 'node:test';
import assert from 'node:assert/strict';
import { scanSource } from '../typescript.mjs';

test('string, regex and template contents do not become comments', () => {
  const source = scanSource('pkg/core.ts', 'const url = "https://example.org"; const expression = /a\\/\\/b/; const text = `/* data */`;\n// ordinary explanation\n');
  assert.equal(source.notes.length, 1);
  assert.equal(source.notes[0].text, '// ordinary explanation');
});

test('JSX comments and JSDoc are both visible', () => {
  const source = scanSource('pkg/view.tsx', '/** prose */\nexport function View() { return <div>{/* hidden note */}text</div>; }');
  assert.deepEqual(source.notes.map(n => n.kind), ['docstring', 'comment']);
  assert.equal(source.notes[1].owner, 'View');
});

test('a source pointer binds the adjacent function', () => {
  const source = scanSource('pkg/core.ts', '// ava:ref pkg/docs/core.ava.okf.md#positive-budget\nexport function budget() { return 1; }');
  assert.equal(source.notes[0].owner, 'budget');
});

test('test definitions use imported runner bindings and exact suite names', () => {
  const source = scanSource('pkg/core.test.ts', "import { describe, it as example } from 'vitest'; describe('budget', () => { example('is positive', () => {}); }); test('not imported', () => {});");
  assert.ok(source.symbols['test:budget::is positive']);
  assert.equal(source.symbols['test:not imported'], undefined);
});

test('skipped tests do not supply evidence', () => {
  const source = scanSource('pkg/core.test.ts', "import { test } from 'vitest'; test.skip('inactive', () => {});");
  assert.equal(source.symbols['test:inactive'], undefined);
});

test('syntax failures do not produce an empty successful scan', () => {
  assert.throws(() => scanSource('pkg/broken.ts', 'function broken( {'), /pkg\/broken.ts/);
});

test('unused strings and forged shebangs cannot hide free prose', () => {
  const source = scanSource('pkg/core.ts', '#! Arbitrary prose\n"Implementation note";\n`Another note`;\n');
  assert.deepEqual(source.notes.map(n => n.kind), ['comment', 'unused-string', 'unused-string']);
});

test('only exact machine directives in their proper position are retained', () => {
  assert.deepEqual(scanSource('pkg/core.ts', "'use client'; export const a = 1;").notes, []);
  assert.deepEqual(scanSource('pkg/core.ts', "#!/usr/bin/env node\n'use strict'; const a = 1;").notes, []);
  assert.equal(scanSource('pkg/core.ts', "const a = 1; 'use client';").notes[0].kind, 'unused-string');
});

test('shadowed bindings and disabled suites cannot supply test definitions', () => {
  const source = scanSource('pkg/core.test.ts', "import { describe, test } from 'vitest'; function helper(test) { test('fabricated', () => {}); } describe.skip('disabled', () => { test('inactive', () => {}); }); describe('active', () => { const test = () => {}; test('shadow', () => {}); });");
  assert.deepEqual(Object.keys(source.symbols).filter(n => n.startsWith('test:')), []);
});

test('Playwright suites keep their labels and disabled suites supply no evidence', () => {
  const source = scanSource('pkg/core.spec.ts', "import { test } from '@playwright/test'; test.describe('suite', () => { test('works', () => {}); }); test.describe.skip('disabled', () => { test('inactive', () => {}); });");
  assert.ok(source.symbols['test:suite::works']);
  assert.deepEqual(Object.keys(source.symbols).filter(n => n.startsWith('test:')), ['test:suite::works']);
});
