# Native Windows gateway

Native Windows Ava runtime is retired. A Windows host can run a gateway inside
WSL2 Linux. See [unattended WSL gateway boot](../conventions/wsl-gateway-boot.md)
for the distribution anchor and Linux service ownership.

On supported POSIX hosts, source-mode fleet updates use
`python -m cli.fleet_update`.

This path remains so older design notes can resolve their references; it does
not describe a native Windows gateway implementation.
