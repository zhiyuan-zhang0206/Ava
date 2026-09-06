# Linux fleet UI gate supervision

On Linux gateways, `ava start` / `ava converge` registers the entry-port gate
as a **systemd user service**. Run these commands as the unit's OS owner.
The service is `com.ava.gate.<escaped-home-slug>.service`, in
`${XDG_CONFIG_HOME:-~/.config}/systemd/user/`. Each AVA home has its own unit;
the checkout's venv interpreter and the explicit `AVA_HOME` select its code
and configuration. The gate runs with a clean environment containing only
HOME, PATH, AVA_HOME and its source hash; credentials stay in the ordinary
AVA configuration files, never in the service definition.

## Host prerequisite

The host must run systemd with a reachable user manager. An unattended gateway
also needs **lingering enabled for its OS owner**, so that manager starts at
boot and survives logout. An operator can inspect it with
`loginctl show-user USER --property=Linger`; configure it through
`sudo loginctl enable-linger USER` when preparing the host. Converge does not
install systemd, change login policy or silently launch an unsupervised gate.
An unavailable manager raises before it changes the unit or stops a legacy
listener. Cron callers connect to the current UID's `/run/user/UID/bus`
without relying on a shell's exported DBus variables.

On WSL, this covers the Linux process only. The Windows host must also keep
the distribution alive and start it after boot. See
[Windows and WSL setup](windows-setup.md); a running user service alone does
not establish no-login Windows recovery.

## Runtime and teardown

| Event | Gate behavior |
|---|---|
| Process crash or unexpected clean exit | systemd restarts it after two seconds; the unit has no start-rate fuse |
| Ordinary `ava cluster update` / `ava restart` | The service-session teardown leaves the gate alone |
| Converge with unchanged unit and active, enabled gate | No rewrite, reload or restart |
| Gate code/assets or launch definition change | Stop the old unit, atomically replace the definition, reload, enable and start |
| Full `ava stop` | Stop and wait; systemd does not restart an explicitly stopped service. The registration remains for the next user-manager boot or converge |
| `ava cluster destroy --path HOME` | Stop, disable and remove only that home's gate unit |

The source hash covers `services/gate/`, as on macOS. A stop failure leaves
the old definition intact; reload/start failure raises and is retried by the
next converge. Mutations for one home share a bounded file lock. Migration
from the former detached process requires its PID to match both the exact
`-m services.gate.daemon` argv pair and the owning checkout's working
directory. Unknown ownership or a surviving process stops convergence;
another listener is never launched over it.

`ava status` reports HTTP entry liveness separately from systemd supervision.
A restart in progress is supervised and can still be temporarily unavailable.
Logs remain at `$AVA_HOME/logs/gate.log`. Health-probe remains an observer;
systemd performs crash recovery. macOS continues to use its launchd job.

The registration's unit syntax and lifecycle state transitions have focused
tests. A production deployment still needs its own crash/recovery and reboot
acceptance; static unit validation does not prove those operational events.

Systemd contracts: [restart and explicit stop](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#Restart=),
[user lingering](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html#enable-linger%20USER%E2%80%A6),
[unit command parsing](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#Command%20Lines).
