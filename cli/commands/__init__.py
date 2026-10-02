"""`ava` CLI command package — one module or subpackage per command domain.

There is no package-level re-export: this `__init__` does no import work, so
`import cli.commands` loads nothing, and importing `cli.commands.X` loads only
X and what X itself imports. Each command module is its own public door —
`cli.parsers` handlers lazy-import their `cmd_*` implementation straight from
the module that defines it (e.g. `from cli.commands.lifecycle.start import cmd_start`),
never from this package namespace. Test seams are patched at that same
defining module (e.g. `cli.commands.lifecycle.status.cmd_status`), never here.
"""
