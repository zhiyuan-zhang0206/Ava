---
type: doc
title: Incremental source note checks
description: Exact note matching within real lexical scopes and consistent Git snapshots.
tags: [scripts, lint, documentation]
---

# Incremental source note checks

`changes.py` compares notes using the current parser on both Git source blobs.
The key is the path, lexical declaration identity, note kind and exact raw token
or string literal. A counter consumes each old occurrence once. Line shifts and
ordinary local assignment changes do not create notes; copies, text/quote changes,
new paths and moves to another lexical declaration do. Git uses `--no-renames`,
so rename/copy similarity cannot grant source text credit across paths.

Two comparisons normalize the project rename instead of the syntax: note text and
paths map the brand tokens (`meiva`/`ava`, and one frozen digest pair) before
matching, and a path that only differs by those tokens is looked up in the base
tree. Without that exception the cutover alone would rewrite every existing note,
and every branch opened before it would fail the gate on unrelated edits. The
normalization is a rename shim, not a general equivalence: it can be removed once
no in-flight branch predates the cutover.

Python uses the same complete declaration collection that resolves machine
rationale. A multiline Python body's final span includes consecutive trailing
comments at its body indentation or deeper, using tab-expanded columns; the next
code line or a shallower nonblank comment ends that tail. Inline suites do not
invent an indented body. This keeps trailing body notes out of the module bucket.
JavaScript/TypeScript additionally records actual anonymous callbacks,
arrow functions and object methods as lexical scopes; these are not new public
code anchors. Typed reference/SDK assignment ownership stays separate. Named
function/class declarations and literal test anchors retain their existing role.

A lexical key includes each real parent, declaration kind, ordinal and total
among siblings of the same kind/name. Sync and async Python functions share a
function group. Getters/setters, overloads and repeated conditional declarations
keep distinct positions without forcing unrelated file text into strict scope.
An inserted/deleted member changes only that repeated group's matching identity.
No line-history or behavioral AST fingerprint is used. Identical text reordered
inside the same identity can match; independent review must catch semantic drift.

For an imported runner's recognized direct suite call with a literal string
label, the real callback's lexical name uses the enclosing suite label chain.
Adding or reordering a differently named suite leaves existing suite identities
stable. Repeated labels under the same parent still use ordinal/total matching;
moving a note across suites cannot reuse its former credit. This creates no
public suite anchor and does not change existing typed test anchors.

Variable/property names and recognized test labels likewise distinguish their
existing callback scopes. Other anonymous callbacks, including dynamic suite
labels and unrecognized or shadowed runner bindings, remain in the anonymous
group of their real parent. Adding/removing a member conservatively rechecks
unchanged notes in that group and its descendants. The checker does not infer
runtime labels, hash function bodies or promise that every code-only edit leaves
all legacy notes outside strict scope.

Both old and target Python/TypeScript text are read from Git blobs without newline
normalization. The same TypeScript compiler accepts filesystem or JSON-supplied
source text; its versioned output includes raw text, lexical identity and hashes.
The Python reader rejects incomplete, stale or unknown parser output. A failure
to parse either source side cannot produce an empty successful comparison.

The shell reads pre-commit's from/to environment at the process boundary and
passes explicit CLI inputs. Python resolves commit identities and requires a
committed range to end at HEAD. Staged comparisons use HEAD/index. Every
parsed note link still uses the tracked checkout, so unstaged changes fail; committed comparisons also reject a staged
index differing from HEAD. Normal pre-commit isolation is the supported way to
check a partial commit. The tool never stashes, resets or constructs a second
checkout. Symlink targets are not source files; a new regular file replacing one
has no old note credit.

`changed` reports new/rewritten note count separately from the selected file count.
`validate` checks every note link, while explicit `check`/`inventory`
remain full strict audits. None proves semantic truth, rule necessity, test
execution or that unchanged prose remains accurate after a behavior change.

Parent: [[scripts/content_lint/code_comments/docs/code_comments.meiva.okf.md]].
