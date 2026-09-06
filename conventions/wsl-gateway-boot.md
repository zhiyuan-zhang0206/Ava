# Unattended boot for a WSL gateway

A WSL gateway needs two boot owners: Windows starts and holds the intended
distribution open; Linux starts the installed Ava home. Ava's native Windows
runner still uses the interactive account described in
[Windows setup](windows-setup.md). Do not change that runner's tasks to make the
WSL gateway boot.

Windows `WSLService` being automatic does not establish a distribution boot
trigger. A logon task needs a logged-on user, and
[systemd services do not themselves keep WSL alive](https://learn.microsoft.com/en-us/windows/wsl/systemd).
The opt-in renderer below produces a separate distribution anchor; it does not
install, register, start, stop, or update anything.

## The two boot owners

1. **Windows Task Scheduler:** an explicit distribution-owning Windows account
   uses S4U, without a stored password or interactive desktop. A boot trigger
   repeats every minute. Its action is the absolute local `wsl.exe` path with
   `--distribution <name> --user <linux-owner> --exec /usr/bin/sleep infinity`.
   `IgnoreNew` retains one running anchor; a later tick starts a new one after
   either a failed or a successful client exit. The task has no execution-time
   limit, battery restriction, or idle-only condition. It does not wake a sleeping
   Windows host.
2. **Linux:** systemd starts the enabled cron daemon. Ava's existing home-scoped
   `@reboot AVA_HOME=... /absolute/checkout/.venv/bin/ava boot` starts its retrying
   bring-up. `ava boot` retries failed starts indefinitely; after success, the
   ordinary Ava watchdogs supervise services. Preserve unrelated crontab entries.

The anchor belongs to the distribution, not an Ava home: it may keep other Linux
workloads alive too. It is deliberately outside `\Ava\<home-slug>\` and is not
removed by `ava cluster destroy`. Do not install competing anchors for the same
distribution. Inventory any existing logon/keepalive wrappers before replacing
them during a coordinated maintenance window.

[S4U has no Windows network or encrypted-file access](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-logontype-principaltype-element).
That restriction alone does not prove whether Linux networking inside WSL works.
Validate the real distribution, local data paths, and required Linux network
operations from that task identity. Do not switch to SYSTEM: WSL distribution
ownership must remain the actual Windows user's. If S4U fails the host's
acceptance test, resolve that evidence before installing an unattended gateway.

## Prepare and inspect without registration

Use the real distribution name from `wsl.exe --list --quiet`, its Linux account,
and the Windows account that owns it. In PowerShell at this checkout:

```powershell
$distribution = 'Ubuntu-24.04'
$linuxUser = 'linux-owner'
$windowsUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$wslExecutable = Join-Path $env:SystemRoot 'System32\wsl.exe'
$xml = (python scripts/render_wsl_boot_task.py `
    --distribution $distribution --linux-user $linuxUser `
    --windows-user $windowsUser --wsl-executable $wslExecutable | Out-String)
if ($LASTEXITCODE -ne 0) { throw 'WSL task rendering failed' }

# Parse using the real scheduler without registering a task.
$scheduler = New-Object -ComObject Schedule.Service
$scheduler.Connect()
$definition = $scheduler.NewTask(0)
$definition.XmlText = $xml
$definition.Principal.UserId
$definition.Principal.LogonType
$definition.Actions.Item(1).Arguments
```

The renderer uses only Python's standard library and can also generate XML on
another machine. It never needs Ava settings, the cluster secret, a Windows
password, or a running Ava installation. Parsing verifies schema acceptance,
not account rights or a successful cold boot.

## Install only during the coordinated host change

First verify the Linux owner's installed home, `systemd=true`, enabled/active
cron, and the exact checkout/home-scoped Ava boot entry. Confirm that the gateway
home will be the sole active data-plane owner before any automatic bring-up.
Use an elevated PowerShell **as the distribution owner** if registering the boot
trigger requires administrator rights; do not run as a different account.

Choose a distinct name after inspecting existing tasks. Registration below does
not use `-Force`, so an existing task is not silently overwritten:

```powershell
$taskName = 'Ava-WSL-Gateway-Ubuntu-24.04'
Register-ScheduledTask -TaskName $taskName -Xml $xml -ErrorAction Stop
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName
Get-ScheduledTaskInfo -TaskName $taskName
```

`Start-ScheduledTask` starts the current instance; it does not fire the
BootTrigger. Do not assume minute retries are armed before that trigger has
actually fired at boot. Verify recovery after the real boot-triggered start.

Keep the exact registered task name in the host's operational record. A running
sleep task proves only the anchor is alive. Verify authenticated gateway status,
direct data-plane readiness, runner connectivity, and a real agent operation
separately. Changing `.wslconfig` requires a coordinated WSL restart; a warm
identity probe does not validate newly configured networking.

## Maintenance, rollback, and acceptance

**Disable the task before stopping its process or shutting down WSL.** Otherwise
the repeating trigger can boot the distribution again during a migration or
repair. Disabling the task does not itself stop the current anchor:

```powershell
Disable-ScheduledTask -TaskName $taskName
Stop-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName
```

Stopping the anchor does not stop the cluster or establish data-plane quiescence.
Follow the [cluster runbook](runbook.md) for the coordinated stop and confirm
other distribution anchors are also quiesced before a planned WSL shutdown.
Remove only this registered task when abandoning the setup; never blanket-delete
Windows tasks or Linux cron entries. After a planned restart, enabling the task
alone need not replay its boot trigger; explicitly start it as well:

```powershell
Enable-ScheduledTask -TaskName $taskName
Start-ScheduledTask -TaskName $taskName
```

Before calling unattended recovery ready, validate a Windows reboot **with no
user login** in the approved maintenance window. Observe the task's correct user
and running state, Linux boot ID and systemd/cron, Ava `boot.log`, authenticated
gateway/data-plane readiness, Linux network access, and runner/agent recovery.
Record the Windows native runner's separate interactive-session dependency.
Test that ending only the anchor makes the repeating trigger recover it, and
that disabling it prevents resurrection during maintenance. Until these checks
pass, describe the result as prepared boot wiring, not verified cold recovery.

The trigger/repetition contract is specified by Microsoft's
[BootTrigger schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-boottrigger-triggergroup-element).
