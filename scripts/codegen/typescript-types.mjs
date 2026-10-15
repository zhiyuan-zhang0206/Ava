import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const require = createRequire(new URL('../../ui/web/package.json', import.meta.url));
const { default: openapiTS, astToString } = require('openapi-typescript');
const ts = require('typescript');

function retainDeprecation(node) {
  const comments = ts.getSyntheticLeadingComments(node) ?? [];
  const text = comments.map(comment => `/*${comment.text}*/`).join('\n');
  const probe = comments.length ? ts.createSourceFile('annotation.ts', `${text}\ndeclare const value: unknown;`, ts.ScriptTarget.Latest, true) : undefined;
  ts.setSyntheticLeadingComments(node, undefined);
  ts.setSyntheticTrailingComments(node, undefined);
  if (probe && ts.getJSDocDeprecatedTag(probe.statements[0])) {
    ts.addSyntheticLeadingComment(node, ts.SyntaxKind.MultiLineCommentTrivia, '* @deprecated ', true);
  }
  ts.forEachChild(node, retainDeprecation);
}

// ava:ref scripts/codegen/docs/types.ava.okf.md#generated-types
export async function generateTypes(schema) {
  const nodes = await openapiTS(schema);
  nodes.forEach(retainDeprecation);
  return astToString(nodes);
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  const [input, output, ...extra] = process.argv.slice(2);
  if (!input || !output || extra.length) throw new Error('Usage: node typescript-types.mjs INPUT.json OUTPUT.ts');
  const result = await generateTypes(pathToFileURL(path.resolve(input)));
  await fs.writeFile(output, result, 'utf8');
}
