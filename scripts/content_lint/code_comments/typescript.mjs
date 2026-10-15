import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const require = createRequire(new URL('../../../ui/web/package.json', import.meta.url));
const ts = require('typescript');

export function scanSource(file, text) {
  const kind = /\.[jt]sx$/.test(file) ? ts.ScriptKind.TSX : /\.[cm]?js$/.test(file) ? ts.ScriptKind.JS : ts.ScriptKind.TS;
  const tree = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  if (tree.parseDiagnostics.length) {
    throw new Error(`${file}: ${tree.parseDiagnostics.map(d => ts.flattenDiagnosticMessageText(d.messageText, ' ')).join('; ')}`);
  }
  const symbols = { '<module>': { name: '<module>', line: 1, end_line: text.split('\n').length, parameters: [], ambiguous: false, callable: false } };
  const definitions = [{ ...symbols['<module>'], key: '<module>', parent: 0, kind: 'module' }];
  const spans = new Map();
  const machineComments = new Set();
  const options = { noLib: true, noResolve: true, allowJs: true };
  const host = ts.createCompilerHost(options);
  host.getSourceFile = name => name === file ? tree : undefined;
  const checker = ts.createProgram([file], options, host).getTypeChecker();
  const testNames = new Set();
  const describeNames = new Set();
  const unused = [];
  const playwrightNames = new Set();
  const binding = node => checker.getSymbolAtLocation(node);
  for (const node of tree.statements) {
    if (!ts.isImportDeclaration(node) || !ts.isStringLiteral(node.moduleSpecifier)) continue;
    if (!['vitest', '@playwright/test', 'node:test'].includes(node.moduleSpecifier.text)) continue;
    const clause = node.importClause;
    if (node.moduleSpecifier.text === 'node:test' && clause?.name) testNames.add(binding(clause.name));
    if (clause?.namedBindings && ts.isNamedImports(clause.namedBindings)) {
      for (const item of clause.namedBindings.elements) {
        const original = item.propertyName?.text ?? item.name.text;
        if (['test', 'it'].includes(original)) testNames.add(binding(item.name));
        if (original === 'describe') describeNames.add(binding(item.name));
        if (original === 'test' && node.moduleSpecifier.text === '@playwright/test') playwrightNames.add(binding(item.name));
      }
    }
  }
  const directSuite = expression => (ts.isIdentifier(expression) && describeNames.has(binding(expression))) || (ts.isPropertyAccessExpression(expression) && expression.name.text === 'describe' && ts.isIdentifier(expression.expression) && playwrightNames.has(binding(expression.expression)));
  const suiteWrapper = expression => {
    if (directSuite(expression)) return true;
    if (ts.isPropertyAccessExpression(expression) || ts.isCallExpression(expression)) return suiteWrapper(expression.expression);
    return false;
  };
  const lineAt = position => tree.getLineAndCharacterOfPosition(position).line + 1;
  const addSymbol = (name, node, callable) => {
    symbols[name] = { name, line: lineAt(node.getStart(tree)), end_line: lineAt(node.end), parameters: [], ambiguous: Object.hasOwn(symbols, name), callable };
  };
  const addDefinition = (name, node, kind, parent) => {
    definitions.push({ name, line: lineAt(node.getStart(tree)), end_line: lineAt(node.end), start: node.getStart(tree), end: node.end, kind, parent });
    return definitions.length - 1;
  };
  const declarationKind = node => {
    if (ts.isClassDeclaration(node) || ts.isClassExpression(node)) return 'class';
    return ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node) || ts.isMethodDeclaration(node) || ts.isGetAccessor(node) || ts.isSetAccessor(node) || ts.isConstructorDeclaration(node) ? 'function' : null;
  };
  const declarationName = node => {
    if (node.name) return node.name.getText(tree);
    if (ts.isConstructorDeclaration(node)) return '<constructor>';
    const parent = node.parent;
    if ((ts.isVariableDeclaration(parent) || ts.isPropertyAssignment(parent) || ts.isPropertyDeclaration(parent)) && parent.name) return parent.name.getText(tree);
    return '<anonymous>';
  };
  const walk = (node, scope = [], describes = [], active = true, lexicalParent = 0) => {
    for (const range of [...(ts.getLeadingCommentRanges(text, node.pos) ?? []), ...(ts.getTrailingCommentRanges(text, node.end) ?? [])]) spans.set(range.pos, range);
    const deprecated = node.name && binding(node.name)?.declarations?.includes(node) ? ts.getJSDocDeprecatedTag(node) : undefined;
    if (deprecated && ts.isJSDoc(deprecated.parent) && text.slice(deprecated.parent.pos, deprecated.parent.end) === '/** @deprecated */') {
      machineComments.add(deprecated.parent.pos);
    }
    let nestedScope = scope;
    let nestedParent = lexicalParent;
    let nestedDescribes = describes;
    let nestedActive = active;
    if (ts.isExpressionStatement(node) && (ts.isStringLiteral(node.expression) || ts.isNoSubstitutionTemplateLiteral(node.expression))) {
      const value = node.expression.text;
      const parent = node.parent;
      const first = (ts.isSourceFile(parent) || ts.isBlock(parent)) && parent.statements[0] === node;
      const directive = ts.isStringLiteral(node.expression) && first && (
        (value === 'use client' && ts.isSourceFile(parent)) ||
        ((value === 'use strict' || value === 'use server') && (ts.isSourceFile(parent) || (ts.isBlock(parent) && ts.isFunctionLike(parent.parent))))
      );
      if (!directive) unused.push(node);
    }
    if (ts.isCallExpression(node) && suiteWrapper(node.expression)) nestedActive = false;
    if ((ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node) || ts.isMethodDeclaration(node)) && node.name && ts.isIdentifier(node.name)) {
      nestedScope = [...scope, node.name.text];
      addSymbol(nestedScope.join('.'), node, !ts.isClassDeclaration(node));
    } else if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) {
      const callable = node.initializer && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer));
      addSymbol([...scope, node.name.text].join('.'), node, Boolean(callable));
      if (callable) nestedScope = [...scope, node.name.text];
    }
    const lexicalKind = declarationKind(node);
    if (lexicalKind) nestedParent = addDefinition(declarationName(node), node, lexicalKind, lexicalParent);
    if (ts.isCallExpression(node) && (ts.isIdentifier(node.expression) || directSuite(node.expression)) && node.arguments.length >= 2 && ts.isStringLiteral(node.arguments[0])) {
      const label = node.arguments[0].text;
      const body = node.arguments[1];
      if (ts.isArrowFunction(body) || ts.isFunctionExpression(body)) {
        if (directSuite(node.expression)) { nestedDescribes = [...describes, label]; nestedActive = active; }
        if (active && ts.isIdentifier(node.expression) && testNames.has(binding(node.expression))) {
          const name = `test:${[...describes, label].join('::')}`;
          addSymbol(name, node, true);
          nestedParent = addDefinition(name, node, 'test', lexicalParent);
        }
      }
    }
    for (const child of node.getChildren(tree)) walk(child, nestedScope, nestedDescribes, nestedActive, nestedParent);
  };
  walk(tree);
  const groups = definitions.map(d => JSON.stringify([d.parent, d.kind, d.name]));
  const totals = new Map();
  for (const group of groups) totals.set(group, (totals.get(group) ?? 0) + 1);
  const ordinals = new Map();
  for (let i = 1; i < definitions.length; i++) {
    const d = definitions[i];
    const ordinal = (ordinals.get(groups[i]) ?? 0) + 1;
    ordinals.set(groups[i], ordinal);
    d.key = JSON.stringify([definitions[d.parent].key, d.kind, d.name, ordinal, totals.get(groups[i])]);
  }
  const lexicalOwner = (position, standalone) => {
    const line = lineAt(position);
    const enclosing = definitions.slice(1).filter(d => d.start <= position && position < d.end);
    if (enclosing.length) return enclosing.sort((a, b) => (a.end - a.start) - (b.end - b.start))[0].key;
    const following = standalone ? definitions.slice(1).filter(d => d.line === line + 1) : [];
    return following.length === 1 ? following[0].key : '<module>';
  };
  const owner = line => {
    const enclosing = Object.values(symbols).filter(s => s.name !== '<module>' && s.line <= line && line <= s.end_line);
    if (enclosing.length) return enclosing.sort((a, b) => (a.end_line - a.line) - (b.end_line - b.line))[0].name;
    const following = Object.values(symbols).filter(s => s.line === line + 1);
    return following.length === 1 ? following[0].name : '<module>';
  };
  const notes = [...spans.values()].filter(span => !machineComments.has(span.pos)).sort((a, b) => a.pos - b.pos).map(span => {
    const value = text.slice(span.pos, span.end);
    const line = lineAt(span.pos);
    return { kind: value.startsWith('/**') ? 'docstring' : 'comment', line, end_line: lineAt(span.end - 1), text: value, raw_text: value, owner: owner(line), lexical_owner: lexicalOwner(span.pos, !text.slice(text.lastIndexOf('\n', span.pos - 1) + 1, span.pos).trim()) };
  });
  for (const node of unused) {
    const line = lineAt(node.getStart(tree));
    notes.push({ kind: 'unused-string', line, end_line: lineAt(node.end - 1), text: node.getText(tree), raw_text: node.getText(tree), owner: owner(line), lexical_owner: lexicalOwner(node.getStart(tree), false) });
  }
  const shebang = ts.getShebang(text);
  if (shebang && !/^#!\/usr\/bin\/(?:env node|node)$/.test(shebang)) notes.push({ kind: 'comment', line: 1, end_line: 1, text: shebang, raw_text: shebang, owner: '<module>', lexical_owner: '<module>' });
  notes.sort((a, b) => a.line - b.line);
  return { path: file, sha256: createHash('sha256').update(text).digest('hex'), symbols, notes };
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  const root = process.cwd();
  const input = fs.readFileSync(0, 'utf8');
  const result = process.argv.includes('--sources-json')
    ? Object.entries(JSON.parse(input)).map(([file, text]) => scanSource(file, text))
    : input.split('\0').filter(file => /\.(?:[cm]?[jt]s|[jt]sx)$/.test(file)).map(file => {
        const full = path.resolve(root, file);
        if (!full.startsWith(`${root}${path.sep}`) || fs.lstatSync(full).isSymbolicLink()) throw new Error(`invalid source path: ${file}`);
        return scanSource(file, fs.readFileSync(full, 'utf8'));
      });
  process.stdout.write(JSON.stringify({ version: 2, sources: result }));
}
