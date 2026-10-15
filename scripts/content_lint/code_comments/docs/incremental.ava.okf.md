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

Both old and target Python/TypeScript text are read from Git blobs without newline
normalization. The same TypeScript compiler accepts filesystem or JSON-supplied
source text; its versioned output includes raw text, lexical identity and hashes.
The Python reader rejects incomplete, stale or unknown parser output. A failure
to parse either source side cannot produce an empty successful comparison.

The shell reads pre-commit's from/to environment at the process boundary and
passes explicit CLI inputs. Python resolves commit identities and requires a
committed range to end at HEAD. Staged comparisons use HEAD/index. Global typed
contracts, references, SDK projections and test bindings still use the tracked
checkout, so unstaged changes fail; committed comparisons also reject a staged
index differing from HEAD. Normal pre-commit isolation is the supported way to
check a partial commit. The tool never stashes, resets or constructs a second
checkout. Symlink targets are not source files; a new regular file replacing one
has no old note credit.

`changed` reports new/rewritten note count separately from the selected file count.
`validate` checks global reference integrity, while explicit `check`/`inventory`
remain full strict audits. None proves semantic truth, rule necessity, test
execution or that unchanged prose remains accurate after a behavior change.

```ava-contract
id = "new-note-scope"
kind = "invariant"
statement = "Incremental strict checks consume exact old note text once within the same path and real lexical declaration identity. New copies, rewrites and cross-owner moves remain strict; unrelated code changes and local assignment renames do not force migration of unchanged notes. Global reference integrity is never grandfathered."
code = ["scripts/content_lint/code_comments/changes.py:new_note_indexes"]
tests = ["scripts/content_lint/code_comments/tests/test_incremental.py::test_unrelated_code_changes_do_not_migrate_legacy_notes", "scripts/content_lint/code_comments/tests/test_incremental.py::test_new_rewritten_copied_or_reowned_notes_are_strict", "scripts/content_lint/code_comments/tests/test_incremental.py::test_duplicate_declarations_keep_separate_occurrence_credit", "scripts/content_lint/code_comments/tests/test_incremental.py::test_duplicate_group_growth_cannot_reuse_an_inserted_ordinal", "scripts/content_lint/code_comments/tests/test_incremental.py::test_global_reference_integrity_survives_legacy_matching", "scripts/content_lint/code_comments/tests/test_incremental.py::test_trailing_body_comments_cannot_move_between_functions", "scripts/content_lint/code_comments/tests/test_incremental.py::test_tab_indented_tail_uses_visual_body_columns", "scripts/content_lint/code_comments/tests/test_incremental.py::test_shallower_tail_comments_belong_to_parent_or_module"]
```

```ava-contract
id = "git-snapshot"
kind = "invariant"
statement = "Changed checks compare actual Git blobs from HEAD/index or a resolved base/HEAD range with a matching tracked checkout. They reject mismatched snapshots, invalid targets and either-side parse failure instead of silently checking an empty or unrelated scope. Rename heuristics cannot reuse another path's legacy notes."
code = ["scripts/content_lint/code_comments/changes.py:snapshot"]
tests = ["scripts/content_lint/code_comments/tests/test_git_scope.py::test_cli_code_only_edit_keeps_existing_notes", "scripts/content_lint/code_comments/tests/test_git_scope.py::test_mismatched_target_snapshot_fails_explicitly", "scripts/content_lint/code_comments/tests/test_git_scope.py::test_full_push_range_catches_earlier_new_prose", "scripts/content_lint/code_comments/tests/test_git_scope.py::test_copy_and_rename_targets_cannot_borrow_another_path", "scripts/content_lint/code_comments/tests/test_git_scope.py::test_crlf_current_and_previous_notes_both_come_from_git_blobs", "scripts/content_lint/code_comments/tests/test_git_scope.py::test_parse_failure_on_either_side_does_not_become_empty_scope"]
```

Parent: [[scripts/content_lint/code_comments/docs/code_comments.ava.okf.md]].
