# Windows hosts

Native Windows Ava services and agent runners are retired. Run Ava on a macOS or
Linux host. On Windows hardware, install Ava inside a WSL2 Linux distribution
and use the Linux setup path in [the runbook](runbook.md).

For a WSL gateway that must start without an interactive login, see
[unattended WSL gateway boot](wsl-gateway-boot.md). That Windows scheduled task
only keeps the Linux distribution running; Ava itself runs inside Linux.
