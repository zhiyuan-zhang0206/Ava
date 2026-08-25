# macOS permission handling

macOS attributes both Full Disk Access (TCC) and Application Firewall (ALF)
decisions to executable identity. A Python or Homebrew upgrade can therefore
produce a new identity even when the command path looks conceptually unchanged.
Use the rules below to keep that churn bounded and observable.

## Stable trigger chains

- Pin Ava's uv-managed Python to 3.12.12 with `uv python pin 3.12.12`. Treat a
  Python upgrade as an identity change that requires re-signing or renewed
  authorization.
- Agent jobs must not use uv Python to run broad `grep`, `find`, or `du` scans
  across macOS-protected directories. Use Spotlight through `mdfind` for indexed
  file discovery and narrow any follow-up reads to the required paths.
- Do not repeatedly compile experimental listener binaries under `/private/tmp`.
  Give a listener a stable path and add it to ALF before running it, or expose the
  listener through an already allowed Python bridge.

Run `ava firewall status` to inspect every declarative ALF entry, its purpose and
glob, each resolved binary, and the Allow/Block/Missing result. `ava converge`
repairs and prunes the manifest. Direct mutation was empirically verified without
elevation on macOS 15.3.1; other releases may require the bounded `sudo -n`
fallback or the manual command printed by converge.

## Permission watcher boundary

The gateway host's launchd permission watcher correlates TCC and ALF log
records, keeps pending/cooldown state in local JSON, and posts firing/resolved
instances to the loopback `/api/alerts` ingest. Alerts use
`source=permission-watcher`, `alertname=permission-prompt`, and warning severity;
the responsible application is the identity label while a varying triggering
tool is display-only summary text. The alerts channel owns the persistent UI
row, bell count, and IM fan-out.

While an incident is pending, repeat prompt records are silent. After resolution,
recurrences remain silent for 12 hours and are still tracked locally so their
resolution does not produce an unmatched IM. There is no 30-minute escalation:
an unresolved alerts row remains visible until it flips to resolved. HTTP
delivery retries once and then logs and drops the event, with no direct-database
fallback. In particular, the watcher never writes `agent_notices`; the
2026-08-25 user ruling classifies permission popups as system events that must
not bind to an agent's notice slot.

## Optional immediate containment: authorize uv Python

This is an **optional stopgap only** when an operator needs to stop current TCC
prompts immediately. The user rejected granting the generic uv interpreter Full
Disk Access as Ava's recommended main line; the unified signing design below is
the intended direction.

1. Open **System Settings > Privacy & Security > Full Disk Access**.
2. Add this exact interpreter and enable it:
   `~/.local/share/uv/python/cpython-3.12.12-macos-aarch64-none/bin/python3.12`.
   In the file picker, press Command-Shift-G to enter the full path if the hidden
   directory is not visible.
3. Restart the affected Ava process so macOS evaluates the authorization again.

Changing the pinned Python version changes the executable identity and can require
repeating this stopgap. Do not treat that repetition as the long-term workflow.

## Main-line design: one Ava signing identity

**Design only — implementation is pending V1 verification and user confirmation.**

The target is one recognizable Ava identity shared by the daemon and its helper,
so TCC can attribute protected access to Ava rather than to a generic uv Python
interpreter. The operator interaction target is one command in the user's
Terminal followed by one **Ava** authorization in System Settings. Child-process
attribution is a hypothesis until the V1 experiment below proves it.

The proposed signing material lives in a dedicated file-based keychain that the
daemon's update pipeline can reuse when it re-signs replacement binaries. Signing
must explicitly select it with `codesign --keychain <path>`. The private key is
owner-readable only (`0400`) and limited to signing use. This makes unattended
updates possible but increases the impact of a daemon-user compromise; key
location, access, rotation, and recovery must be reviewed before implementation.

Signing must be initiated from the user's interactive Terminal session. Existing
experiments show that background or SSH signing can fail with
`errSecInternalComponent`, so an implementation must not hide the initial signing
step inside launchd or a remote update process.

### V1 verification gate

Before promising one-time authorization, run a small user-assisted experiment:

1. Create the candidate self-signed Ava certificate and sign a test daemon and
   helper from the user's Terminal.
2. Have the signed daemon launch the helper and access a protected directory.
3. Inspect the TCC `AUTHREQ_ATTRIBUTION` record and require its requesting
   identifier to be `com.ava.*`, rather than the underlying interpreter or helper
   path.

If V1 succeeds and the user confirms the trade-off, the implementation phase must
produce: one Terminal signing command, one-time **Ava** Full Disk Access guidance,
and a verification script that checks the effective signature and TCC attribution.
No signing or certificate provisioning is implemented by the current change.
