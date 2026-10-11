# Remove Windows and WSL support

## Context

Windows reached Ava in three steps. [`2026-06-18-windows-wsl2-docker-path.md`](2026-06-18-windows-wsl2-docker-path.md)
chose WSL2 plus a Docker Compose data plane. [`2026-07-28-windows-agent-runner-only.md`](2026-07-28-windows-agent-runner-only.md)
scoped a native Windows unit to `agent-runner`, and
[`2026-09-28-retire-windows-docker-compose.md`](2026-09-28-retire-windows-docker-compose.md)
retired the compose file, leaving a gateway on Windows hardware inside WSL2.
Native Windows service startup was retired before this record; what stayed was
the code that served it: platform branches (`is_windows`, `SIGBREAK`, the
EDB Postgres path, the Windows venv layout, `CREATE_NO_WINDOW`), AF_UNIX
capability gates that only a Windows host could fail, the `ava boot` retry loop
for schedulers that cannot retry, the WSL boot-task renderer, and the setup
guides for both shapes. The desktop app also built and signed a Windows
installer.

Neither shape is a supported deployment any more, so all of that code protects
a deployment that is not supported.

## Decision

User ruling (2026-10-11): Ava supports macOS and Linux only. Windows and WSL
are removed, not parked.

- Runtime code: the platform predicates and constants, the Windows branches in
  process, file, signal, logging and release code, the Windows venv layout, the
  EDB Postgres path, the Windows OpenSSH and collector artifacts.
- Capability gates that existed only for a host without `AF_UNIX`: the
  `unix_socket` MCP requirement and the roster gates behind it.
- `ava boot` and `cli/boot_retry.py`: macOS delegates retry to launchd and
  Linux to the systemd boot unit, and nothing registered the command.
- The WSL boot-task renderer, the WSL and Windows setup guides, the Windows
  gateway stub, and the Google Drive `/mnt/<letter>` candidates.
- The desktop app's Windows build, signing and NSIS release lane (a separate
  change).

## Alternatives rejected

- **Keep the WSL path as the documented Windows route.** Nothing exercises it;
  the guides described a bring-up that no machine performs, and every
  `win32` branch next to a POSIX one doubled the paths a change must reason
  about.
- **Keep the platform predicates as a seam for a future port.** A port would
  need new supervision, session and signal adapters anyway; dead branches
  written against a retired design are not a head start.

## Not removed

Frozen decision and postmortem records that describe Windows or WSL stay as
written. Input handling that tolerates Windows-authored content (CRLF and BOM in
skills, backslash rejection in URL path segments) stays: it validates data, not
a platform.
