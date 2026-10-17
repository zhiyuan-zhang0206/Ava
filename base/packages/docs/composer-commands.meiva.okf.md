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

Ordinary text bypasses catalog discovery. Disabled commands do not read skills or command files. Enabled discovery reads the current skill source and command files on each call; discovery failures remain visible to command callers.

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

Discovery merges active skills and command files in override order, with later identical names winning, and returns names sorted. File commands override same-named skills. Names display colon-separated namespaces with dash spelling; imported argument-hint metadata is accepted. Malformed frontmatter is skipped, while missing directories are empty.

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

Expansion is source-neutral and produces one string: known command segments expand in typed order with their own free text, joined by a blank line. Unknown leading names pass through; unknown later slash tokens remain argument text. Every command follows the same chaining rules, including lifecycle commands and repeated commands. Skills expand to load-and-follow instructions rather than copied skill bodies.

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
