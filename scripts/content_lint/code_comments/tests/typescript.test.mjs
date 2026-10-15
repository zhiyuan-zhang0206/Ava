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

test('only compiler-attached exact deprecation markers are machine syntax', () => {
  assert.deepEqual(scanSource('api.ts', '/** @deprecated */\nexport const oldValue = 1;').notes, []);
  assert.equal(scanSource('api.ts', '/** @deprecated */\nvoid 0;').notes.length, 1);
  const source = [
    'export interface Item {',
    '  /** @deprecated */',
    '  oldValue: string;',
    '  /** @deprecated Use newValue instead. */',
    '  described: string;',
    '  /** @deprecated @description free prose */',
    '  mixed: string;',
    '}',
    '/** @deprecated */',
  ].join('\n');
  const result = scanSource('probe.ts', source);
  assert.deepEqual(result.notes.map(note => note.line), [4, 6, 9]);
});

test('lexical identities ignore ordinary assignments but distinguish declarations', () => {
  const before = scanSource('core.ts', 'function f() {\n const old = 1; // Keep reserve\n return old;\n}');
  const after = scanSource('core.ts', 'function f() {\n const renamed = (\n  2); // Keep reserve\n return renamed;\n}');
  assert.equal(before.notes[0].lexical_owner, after.notes[0].lexical_owner);
  const moved = scanSource('core.ts', 'function f() {}\nfunction g() {\n // Keep reserve\n}');
  assert.notEqual(before.notes[0].lexical_owner, moved.notes[0].lexical_owner);
});

test('duplicate declarations and getter setter groups keep distinct ordinal identities', () => {
  for (const source of [
    'function f() {\n// First\n}\nfunction f() {\n// Second\n}',
    'class C {\nget value() {\n// First\nreturn 1;\n}\nset value(x) {\n// Second\n}\n}',
  ]) {
    const notes = scanSource('core.ts', source).notes;
    assert.notEqual(notes[0].lexical_owner, notes[1].lexical_owner);
    assert.equal(notes[0].raw_text, '// First');
  }
});

test('same-name group growth changes only that declaration group identity', () => {
  const source = 'function f() {\n// First\n}\nfunction f() {}\nfunction stable() {\n// Stable\n}';
  const before = scanSource('core.ts', source).notes;
  const after = scanSource('core.ts', 'function f() {}\n' + source).notes;
  assert.notEqual(before[0].lexical_owner, after[0].lexical_owner);
  assert.equal(before[1].lexical_owner, after[1].lexical_owner);
});

test('raw multiline text preserves CRLF and comments on module code do not borrow next function', () => {
  const source = scanSource('core.ts', 'const x = 1; // Module\r\nfunction f() {\r\n/* first\r\nsecond */\r\n}');
  assert.equal(source.notes[0].lexical_owner, '<module>');
  assert.equal(source.notes[1].raw_text, '/* first\r\nsecond */');
});

test('actual anonymous callbacks, object methods and arrows do not share note credit', () => {
  for (const source of [
    'run(() => { /* First */ }); run(() => { /* Second */ });',
    'const a = { f() { /* First */ } }; const b = { f() { /* Second */ } };',
    'const a = () => { /* First */ }; const b = () => { /* Second */ };',
    'function a() { /* First */ } function b() { /* Second */ }',
  ]) {
    const notes = scanSource('core.ts', source).notes;
    assert.equal(notes.length, 2);
    assert.notEqual(notes[0].lexical_owner, notes[1].lexical_owner);
  }
});

test('string/computed methods and class expressions have real lexical scopes without new anchors', () => {
  for (const source of [
    "const obj = { 'a'() { /* First */ }, 'b'() { /* Second */ } };",
    'const obj = { [first]() { /* First */ }, [second]() { /* Second */ } };',
    'const A = class { /* First */ }; const B = class { /* Second */ };',
    'export default class { a() { /* First */ } b() { /* Second */ } }',
    'const C = class { a() { /* First */ } b() { /* Second */ } };',
  ]) {
    const notes = scanSource('core.ts', source).notes;
    assert.equal(notes.length, 2);
    assert.notEqual(notes[0].lexical_owner, notes[1].lexical_owner);
    assert.notEqual(notes[0].lexical_owner, '<module>');
  }
  assert.equal(scanSource('core.ts', 'const obj = { [key]() {} };').symbols['[key]'], undefined);
});

test('static suite labels survive unrelated suite additions and reordering', () => {
  for (const [imports, suite, example] of [
    ["import { describe, it } from 'vitest';", 'describe', 'it'],
    ["import { describe as group, test as example } from 'node:test';", 'group', 'example'],
    ["import { test } from '@playwright/test';", 'test.describe', 'test'],
  ]) {
    const stable = `${suite}('stable', () => { /* Suite */ ${example}('works', () => { /* Test */ }); });`;
    const other = `${suite}('other', () => {});`;
    const before = scanSource('core.test.ts', imports + stable);
    for (const source of [imports + stable + other, imports + other + stable]) {
      const after = scanSource('core.test.ts', source);
      assert.deepEqual(after.notes.map(n => n.lexical_owner), before.notes.map(n => n.lexical_owner));
      assert.deepEqual(Object.keys(after.symbols), Object.keys(before.symbols));
    }
  }
});

test('nested suite labels keep their real parent and stable sibling identities', () => {
  const imports = "import { describe } from 'vitest';";
  const before = scanSource('core.test.ts', imports + "describe('outer', () => { describe('inner', () => { /* Keep */ }); });");
  const after = scanSource('core.test.ts', imports + "describe('outer', () => { describe('new', () => {}); describe('inner', () => { /* Keep */ }); });");
  const moved = scanSource('core.test.ts', imports + "describe('other', () => { describe('inner', () => { /* Keep */ }); });");
  assert.equal(after.notes[0].lexical_owner, before.notes[0].lexical_owner);
  assert.notEqual(moved.notes[0].lexical_owner, before.notes[0].lexical_owner);
});

test('identical notes moved across suites cannot borrow their original identity', () => {
  const imports = "import { describe } from 'vitest';";
  const before = scanSource('core.test.ts', imports + "describe('left', () => { /* Keep */ }); describe('right', () => {});");
  const after = scanSource('core.test.ts', imports + "describe('left', () => {}); describe('right', () => { /* Keep */ });");
  assert.notEqual(after.notes[0].lexical_owner, before.notes[0].lexical_owner);
  assert.deepEqual(Object.keys(after.symbols), ['<module>']);
});

test('duplicate suite label growth invalidates only its own sibling group', () => {
  const imports = "import { describe } from 'vitest';";
  const source = "describe('same', () => { /* First */ }); describe('same', () => {}); describe('stable', () => { /* Stable */ });";
  const before = scanSource('core.test.ts', imports + source);
  const after = scanSource('core.test.ts', imports + "describe('same', () => {});" + source);
  assert.notEqual(after.notes[0].lexical_owner, before.notes[0].lexical_owner);
  assert.equal(after.notes[1].lexical_owner, before.notes[1].lexical_owner);
});

test('unproved suite labels retain conservative anonymous identities', () => {
  for (const [prefix, stable, addition, suffix] of [
    ["import { describe } from 'vitest';", 'describe(label, () => { /* Keep */ });', 'describe(other, () => {});', ''],
    ['function helper(describe) {', "describe('stable', () => { /* Keep */ });", "describe('new', () => {});", '}'],
  ]) {
    const before = scanSource('core.test.ts', prefix + stable + suffix);
    const after = scanSource('core.test.ts', prefix + addition + stable + suffix);
    assert.notEqual(after.notes[0].lexical_owner, before.notes[0].lexical_owner);
  }
});
