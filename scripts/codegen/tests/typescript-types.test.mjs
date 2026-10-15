import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { generateTypes } from '../typescript-types.mjs';

const require = createRequire(new URL('../../../ui/web/package.json', import.meta.url));
const { default: openapiTS, astToString } = require('openapi-typescript');
const ts = require('typescript');

const schema = {
  openapi: '3.1.0',
  info: { title: 'Type projection', version: '1' },
  paths: {},
  components: {
    schemas: {
      Item: {
        type: 'object',
        description: 'Keep this description in OpenAPI.',
        required: ['kind', 'url'],
        properties: {
          kind: { type: 'string', enum: ['ready', 'failed'], description: 'State.' },
          url: { type: 'string', const: 'https://example.test/a/*literal*/' },
          optional: { type: ['string', 'null'], default: null, deprecated: true },
          items: { type: 'array', items: { $ref: '#/components/schemas/Item' } },
        },
      },
    },
  },
};

function typeStructure(text) {
  const source = ts.createSourceFile('types.ts', text, ts.ScriptTarget.Latest, true);
  assert.equal(source.parseDiagnostics.length, 0);
  return ts.createPrinter({ removeComments: true }).printFile(source);
}

test('preserves the upstream type structure and schema while omitting prose', async () => {
  const original = structuredClone(schema);
  const upstream = astToString(await openapiTS(schema));
  const actual = await generateTypes(schema);
  assert.match(upstream, /Keep this description in OpenAPI/);
  assert.doesNotMatch(actual, /Keep this description|@description|@enum/);
  assert.equal(typeStructure(actual), typeStructure(upstream));
  assert.match(actual, /\/\*\* @deprecated \*\//);
  const parsed = ts.createSourceFile('types.ts', actual, ts.ScriptTarget.Latest, true);
  const deprecated = [];
  function collect(node) {
    if (ts.getJSDocDeprecatedTag(node)) deprecated.push(node.name.getText(parsed));
    ts.forEachChild(node, collect);
  }
  collect(parsed);
  assert.deepEqual(deprecated, ['optional']);
  assert.match(actual, /https:\/\/example\.test\/a\/\*literal\*\//);
  assert.deepEqual(schema, original);
});

test('matches the checked-in projection of the complete API schema', async () => {
  const input = new URL('../../../ui/web/openapi.json', import.meta.url);
  const output = new URL('../../../ui/web/src/lib/contracts/types-generated.ts', import.meta.url);
  const expected = await generateTypes(input);
  assert.equal(await fs.readFile(output, 'utf8'), expected);
  const upstream = astToString(await openapiTS(input));
  assert.equal(typeStructure(expected), typeStructure(upstream));
});

test('rejects an unresolved schema reference', async () => {
  const invalid = structuredClone(schema);
  invalid.components.schemas.Item.properties.items.items.$ref = '#/components/schemas/Missing';
  await assert.rejects(generateTypes(invalid), /Can't resolve/);
});
