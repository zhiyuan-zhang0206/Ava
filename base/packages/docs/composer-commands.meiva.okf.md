---
type: doc
title: Composer commands
description: Live command discovery and lazy, source-neutral prompt expansion.
tags: [base, commands]
---

# Composer commands

`composer_commands.py` is framework-internal. The claim node expands Composer
text; gateway autocomplete and `meiva.agents.commands()` share its catalog.
Composition roots supply the active skill reader through `CommandCatalog`.

## Catalog boundary

```meiva-contract
id = "catalog-lazy-boundary"
kind = "invariant"
statement = "Ordinary text bypasses catalog discovery. Disabled commands do not read skills or command files. Enabled discovery reads the current skill source and command files on each call; discovery failures remain visible to command callers."
code = ["base/packages/composer_commands.py:CommandCatalog", "base/packages/composer_commands.py:_commands_enabled"]
tests = ["base/packages/tests/test_composer_commands.py::test_catalog_plain_text_does_not_read_skills", "base/packages/tests/test_composer_commands.py::test_catalog_plain_text_does_not_read_command_files", "base/packages/tests/test_composer_commands.py::test_expand_passthrough_when_disabled", "base/packages/tests/test_composer_commands.py::test_the_catalog_reads_its_skill_source_on_every_call", "base/packages/tests/test_composer_commands.py::test_catalog_reads_command_files_afresh"]
```

Ordinary text means a message that does not start with `/`; a slash inside prose
or after leading whitespace is not an invocation. This check belongs before
discovery, so an unavailable skill registry or unreadable command file cannot
break unrelated chat. The pure `expand_command` function retains the same
text rule for callers that already hold a catalog. Command discovery remains
live: caching it would hide installations and file edits from a running process.

The enabled setting is profile-aware. Agent processes read the agent config
domain. Gateway processes do not construct that domain, so the guarded fallback
reads `MEIVA_COMMANDS_ENABLED` from the cluster `.env` file and defaults to enabled
when absent. That profile fallback is existing behavior; the lazy-boundary tests
exercise the agent-domain setting, not a full gateway startup.

## Discovery and names

```meiva-contract
id = "command-discovery"
kind = "invariant"
statement = "Discovery merges active skills and command files in override order, with later identical names winning, and returns names sorted. File commands override same-named skills. Names display colon-separated namespaces with dash spelling; imported argument-hint metadata is accepted. Malformed frontmatter is skipped, while missing directories are empty."
code = ["base/packages/composer_commands.py:command_dirs", "base/packages/composer_commands.py:scan_dir", "base/packages/composer_commands.py:skill_commands", "base/packages/composer_commands.py:discover_commands"]
tests = ["base/packages/tests/test_composer_commands.py::test_discover_dedups_later_source_wins", "base/packages/tests/test_composer_commands.py::test_discover_sorted_by_name", "base/packages/tests/test_composer_commands.py::test_explicit_command_overrides_skill_as_command", "base/packages/tests/test_composer_commands.py::test_builtin_source_ignores_legacy_root_and_keeps_user_override", "base/packages/tests/test_composer_commands.py::test_skill_carried_commands_walk_the_skills_it_is_given", "base/packages/tests/test_composer_commands.py::test_scan_dir_renders_canonical_namespace_spelling", "base/packages/tests/test_composer_commands.py::test_scan_dir_reads_claude_code_argument_hint", "base/packages/tests/test_composer_commands.py::test_scan_dir_skips_malformed_frontmatter", "base/packages/tests/test_composer_commands.py::test_scan_dir_missing_dir_is_empty", "base/packages/tests/test_composer_commands.py::test_discover_empty_when_disabled"]
```

Sources, from lowest to highest precedence:

1. Every active skill as a same-named command.
2. `<repo>/meiva_builtins/commands/`, with bare names.
3. `<MEIVA_HOME>/commands/`, with bare names.
4. Each built-in plugin's `commands/`, under its plugin namespace.
5. Each installed plugin's `commands/`, under its plugin namespace.
6. Each supplied active skill's `commands/`, under that skill's complete namespace.

Skill sources inherit the caller's install-registry gating. The legacy
`<repo>/commands/` directory is ignored. Namespace display follows the skill tree:
`plugin:item` or `a:b:item`. Matching accepts dotted and underscore spellings;
expansion echoes the caller's spelling.

A command file has optional `description` and `instruction-hint` frontmatter;
`argument-hint` is accepted for Claude Code plugin interoperability. The hint
describes one free-text argument, not parsed positional arguments. A file
without frontmatter uses its entire body. Malformed frontmatter is logged and
skipped so an authoring error does not remove otherwise valid picker entries.
Read and decoding failures are not classified as malformed frontmatter and
continue to fail enabled discovery.

## Expansion inside one message

```meiva-contract
id = "command-expansion"
kind = "invariant"
statement = "Expansion is source-neutral and produces one string: known command segments expand in typed order with their own free text, joined by a blank line. Unknown leading names pass through; unknown later slash tokens remain argument text. Every command follows the same chaining rules, including lifecycle commands and repeated commands. Skills expand to load-and-follow instructions rather than copied skill bodies."
code = ["base/packages/composer_commands.py:Command", "base/packages/composer_commands.py:Invocation", "base/packages/composer_commands.py:split_commands", "base/packages/composer_commands.py:_expand_one", "base/packages/composer_commands.py:expand_command"]
tests = ["base/packages/tests/test_composer_commands.py::test_chain_is_exactly_the_single_expansions_concatenated", "base/packages/tests/test_composer_commands.py::test_chain_expands_every_command_in_the_order_typed", "base/packages/tests/test_composer_commands.py::test_chain_gives_each_command_its_own_argument", "base/packages/tests/test_composer_commands.py::test_unknown_leading_name_passes_the_whole_chain_through", "base/packages/tests/test_composer_commands.py::test_slash_token_that_is_not_a_command_stays_in_the_argument", "base/packages/tests/test_composer_commands.py::test_missing_space_between_commands_is_one_unknown_name", "base/packages/tests/test_composer_commands.py::test_lifecycle_command_chains_like_any_other", "base/packages/tests/test_composer_commands.py::test_repeating_one_command_expands_it_twice", "base/packages/tests/test_composer_commands.py::test_command_record_carries_no_chaining_policy", "base/packages/tests/test_composer_commands.py::test_expand_is_source_neutral", "base/packages/tests/test_composer_commands.py::test_skill_as_command_namespaced", "base/packages/tests/test_composer_commands.py::test_underscore_command_spelling_still_expands"]
```

One send remains one message. Splitting a chain into separate messages would
allow separate claim turns to answer without the other commands' context or
ordering. The mechanism adds no chain preamble, classification, refusal or
special warning. For example, `/compact` describes replacing context, but the
meaning of a following prompt is left to the agent interpreting those prompts.
The command record carries no per-command chaining policy.

Only a registered slash token at the beginning opens a chain. Later registered
tokens after whitespace open new segments. `/recap check /path/to/file` retains
the path as free text; `/recap/review` is one unknown token. Each invocation
retains the typed name and its following free text. A skill invocation directs
the agent to `meiva.help(meiva.skills.<target>)`, keeping the skill and its references
as the source of truth.

Both human and peer-agent chat pass through this expansion. The envelope owns
sender attribution, so the expansion must not inject a human-specific actor.
