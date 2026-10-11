---
type: doc
title: Operator database composition
description: Command-owned lazy database handles retain one explicit operator exemption.
tags: [cli, database]
---

# Operator database composition

`cli/database.py:operator_database_factory()` returns one command-owned lazy
constructor. All handles returned by that constructor share an exempt
`ProcessDbGate`. Construction reads neither Settings nor Git; the first handle
imports the dial stack, and each handle reads the live connection settings.

Use that same factory for the command's work and telemetry writer. The lock
retains one gate even when their first calls overlap. Separate commands own
separate factories. The exemption belongs to these handles; it never changes
another process or service gate. An operator can stop stale code without a Git
checkout, while service entries retain their captured-image admission.

The entry constructs the factory after parsing and passes it through the parsed
command to every handle consumer. Import and help retain their lightweight
boundaries; init and start admit the home before configuration-dependent work.
`operator_event_pipeline(factory)` retains one lazy pipeline for logging and
health initialization and cold command reporters. Its database writer borrows
this same factory. Producer ports are `Callable[[], EventPipeline]`; the
ordinary callable owner retains `EventPipeline | None` and returns the original
`EventPipeline`. `close` returns its `DrainResult`, or `None` for a quiet owner;
`main` closes only a constructed writer within a finite budget, without opening
a writer for quiet or help paths. Unfinished writers remain owned and are
reported; an original worker error propagates, or becomes a note on the command's
primary exception. Existing emitter bindings still close their downstream
process sinks at normal exit.

The callable `OperatorDatabaseFactory` also supplies `for_url(url)` to backup
verification. That method replaces only the live connection slice's URL and
retains the same operator gate. CLI backup roots pass this callback explicitly;
the restored database never receives a fresh gate or a process-wide exemption.

CLI business tests borrow a real factory through the explicit, non-autouse
`tests/path_scoped/cli_tests.py:operator_database` fixture. Context and harness
builders receive that input rather than constructing another operator. Tests
outside the existing fixture scopes import this fixture explicitly; they do not
extend scope declarations or inherit unrelated autouse inputs. Runtime CI
dependency analysis follows these fixture and helper imports back to this owner.
