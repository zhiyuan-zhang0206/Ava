# Ava — project guidance for coding agents

Read [docs/contributing.md](docs/contributing.md) before repository work. It is
the contributor entry point for setup, scoped validation, self-review and PRs.
Follow the user's authorized scope. Before discussing or reviewing code, read
the revision in question: the named branch, PR or worktree; the deployed
generation for runtime behavior; otherwise a freshly fetched `origin/main`
(read with `git show` or a new worktree, never by pulling a deployment
checkout). If you cannot fetch, name the revision you read.

## Project invariants

- Keep the core small. Ava uses one `execute_code(code: str)` tool and the
  `ava.*` namespace; see [philosophy](docs/conventions/philosophy.md).
- Fail fast on invalid model input and unknown enum values. Validate raw inputs
  at their boundary; do not silently replace required data with defaults.
- Prefer established dependencies over parallel implementations. Supported
  runtimes are Python 3.12, Postgres 17 and Redis 8.2; runtime upgrades require
  an explicit project decision. No beta/nightly dependencies.
- Source, comments, documentation, prompts and errors are English. Frontend
  i18n locale files are the only raw-CJK exception.
- Keep one owner for shared facts and vocabulary. Respect the enforced import
  contracts and generate derived schema/types/constants from their owners.
- A public SDK entry must guard an invariant or make a frequent operation
  shorter or harder to misuse; pure aliases are removed. See
  [when a public SDK entry earns its place](docs/conventions/python-conventions.md#when-a-public-sdk-entry-earns-its-place).
- Trace consumers before changing a contract; do not widen local checks into
  unrelated suites.
- Development is separate from a running deployment. Use an isolated checkout,
  its own real virtualenv and a temporary `AVA_HOME` for development tools that
  import application code. Pytest supplies its own isolation.
- A merge does not authorize deployment. Production changes require separate
  operator authorization and verification of the actual running generation.
- Merged migrations are immutable. Fix forward, synchronize `db/schema.sql`,
  and use expand-contract for lossy changes. See the [migration guide](db/docs/migrations.md).

## Code and documentation boundaries

- [Python conventions](docs/conventions/python-conventions.md) own coding and
  structure rules: import direction, file/directory and function budgets.
- [Import layering](docs/conventions/import-layering.md) explains the layering the
  `meizel.toml` manifests state; [technology selection](docs/conventions/technology-selection.md)
  explains dependency choices.
- [OKF index](docs/index.ava.okf.md) and component-local `docs/` describe the
  current system. Update the relevant owner in the same PR as the code.
- [Documentation maintenance](docs/conventions/doc-maintenance.md) owns placement:
  current cross-cutting rules in `docs/conventions/`, choices in
  `docs/decisions/`, incident analyses in `docs/postmortems/`, and plans in `future/`.
  Historical records are evidence, not current contributor instructions.
- [Defensive patterns](docs/conventions/defensive-patterns.md) explain recurring
  failure classes; read the relevant patterns before lifecycle or infra changes.

## Find the right reference

| Task | Reference |
|---|---|
| Set up development | [Development setup](docs/conventions/dev-setup.md) |
| Run relevant tests | [Testing](docs/conventions/testing.md) |
| Review this contribution | [review-contribution](.agents/skills/review-contribution/SKILL.md) |
| Change the database | [Migration guide](db/docs/migrations.md) |
| Maintain SDK docstrings | [SDK docstrings](docs/conventions/sdk-docstring-discipline.md) |
| Understand runtime operations | [Runbook](docs/conventions/runbook.md) |

Before moving or changing a symbol, use
`.venv/bin/python scripts/audit/where_used.py TARGET` (`pkg.mod:name`, a module
or a path). After a module move, use `scripts/audit/module_moves.py OLD=NEW`.
Repository skills live in `.agents/skills/`; built-in mirrors point into
`ava_builtins/skills/`. Load an applicable skill when its capability is needed,
not merely because a process name appears in a document.
