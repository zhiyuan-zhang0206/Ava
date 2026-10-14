---
type: doc
title: Operator database composition
description: Command-owned lazy database handles retain one explicit operator exemption.
tags: [cli, database]
---

# Operator database composition

`cli/database/__init__.py:operator_database_factory` returns one command-owned lazy constructor:

```python
factory = operator_database_factory(
    local_host=command.machine.host,
    gateway_url_reader=lambda: command.config.view.gateway.gateway_url,
    open_database=open_operator_database,
)
```

All handles returned by that constructor share an exempt
`ProcessDbGate`. Construction reads neither Settings nor Git; the first handle
imports the dial stack, and each handle reads the live connection settings.

`open_database(config, gate, direct_url)` builds each handle and is required: the entry
(`cli.main.main`) passes `open_operator_database`, which constructs the real `Database`, and a test
passes a recorder to observe the gate and configuration a handle receives, or a function that raises
to see the construction error unchanged, without patching anything.

The one exception is the gateway login. A start that births (or first admits) the database
authority has no login at boot, so the data-plane step calls
`OperatorDatabaseFactory.adopt_login(url)` once the ledger exists. Handles built afterwards, and
the command's event writer built on the same factory, dial that URL; `db_url()` names it. Adopting
the URL already adopted changes nothing, and a different one raises: a process whose login changes
twice no longer knows which generation it acts as. Handles built before the adoption keep the URL
they were built with.

Use that same factory for the command's work and telemetry writer. The lock
retains one gate even when their first calls overlap. Separate commands own
separate factories. The exemption belongs to these handles; it never changes
another process or service gate. An operator can stop stale code without a Git
checkout, while service entries retain their captured-image admission.

The entry constructs the factory after parsing and passes it through the parsed
command to every handle consumer. The callback stays lazy and belongs to the
same `CommandRuntime.machine` used by logging and telemetry. Import and help retain their lightweight
boundaries; init and start admit the home before configuration-dependent work.
The required gateway URL reader belongs to that same command's `ConfigBoot`; the factory binds
it to each resolver without reading it. Only a remote pooled direct dial needs its value.
`operator_event_pipeline(factory, machine=command.machine, home=ava_home())` retains one lazy pipeline for logging and
health initialization and cold command reporters; `OperatorEventPipeline(..., flush_interval_s=)` sets how long its writer holds a
partial batch (0.5 seconds by default). Its database writer borrows
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
