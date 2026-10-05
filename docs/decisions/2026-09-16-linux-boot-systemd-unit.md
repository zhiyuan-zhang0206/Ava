# Linux boot convergence: a distro-level systemd unit as the boot owner

## Context

The Linux boot path was a user-crontab `@reboot` entry
(`shared/os_autostart`): cron fires the line exactly once, so the retry loop
lives inside a child process (`cli/boot_retry.py`, `ava boot`) that nothing
supervises — nothing restarts it if it dies or wedges, and a single convergence
attempt that hangs blocks the next one. macOS gets this property from launchd
(`KeepAlive`); on Linux the supervisor of record is systemd, which the cron
entry never used. On 2026-09-16 a WSL gateway stayed un-booted across a
distro-level restart loop after an aborted rollout (investigation continues
under task #3593). Whatever the convergence root causes, the boot path itself
exposed no supervised retry and no operator-visible state. The policy every
platform must state is in `shared/boot_policy.py`: retry every
`BOOT_RETRY_INTERVAL_S`, no attempt cap.

## Decision

On a host whose service manager is systemd, the boot path is a **system**
(distribution-level) unit `ava-boot.<home-slug>.service`, installed by
`ava cluster boot-unit install` (`shared/os_boot_unit.py`):

- `ExecStart` is `$AVA_HOME/bin/ava-boot-converge.sh`, a one-attempt
  convergence script (`ava start --no-readiness-gate`); exit 0 means converged,
  non-zero means the unit retries — the exit code IS the retry contract.
- The retry is systemd's: `Restart=on-failure`, `RestartSec=60`,
  `StartLimitIntervalSec=0` (no attempt cap), with `RuntimeMaxSec=900` killing
  a wedged attempt so a hang cannot block the next one.
- A system unit, not `systemctl --user`: no login-session or linger dependency,
  observable in `systemctl` / journald without a session, still running as the
  cluster's own user (`User=`), so file ownership is unchanged.
- Exactly one boot owner: once the unit is installed AND enabled,
  `_register_linux` leaves the crontab entry out and removes a stale one; the
  enable step itself removes it in the same call. A staged install
  (`--no-enable`) keeps the crontab entry live until the switch.
- Hosts whose service manager is not systemd keep the crontab + `ava boot`
  path unchanged.

## Alternatives rejected

- **Keep the @reboot cron only.** The scheduler fires once and cannot retry;
  the retry child is unsupervised by construction — the gap that motivated this
  decision.
- **Keep cron and add a stripped systemd service as a second entry.** Two
  owners for one job race two converge runs at every boot.
- **`systemctl --user` unit.** Depends on a login session or linger
  bookkeeping; boot convergence must not.
- **Only fix the convergence/rollout logic.** Necessary but orthogonal: a
  supervisor bounds any attempt and gives retries a restart path, independent of
  why an attempt failed.

## Consequences

- Privileged steps (`/etc/systemd/system`, enable/start) go through `sudo -n`,
  same no-prompt stance as `shared.macos_firewall`; failures raise actionably.
- The unit's enable state is per-home state; `ava cluster boot-unit status`
  reports it (unit/script/state/cron-entry), and `ava cluster destroy` removes
  the unit with the other OS jobs.
- Drills and reviewed rollouts can stage the files (`--no-enable`) without
  switching the live boot path.
