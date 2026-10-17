# Shared browser operations

The [shared browser owner](../../../docs/agent_runner_side/browser/browser/browser.meiva.okf.md)
describes Chrome custody, the shared MCP upstream, native probes and teardown.
The [in-daemon MCP client](../../../../meiva/mcps/docs/mcps.meiva.okf.md) owns client dispatch;
the operating choices below do not redefine either component.

- **Profile source — fresh vs. seeded from your daily Chrome**: the dedicated
  profile is normally created empty, so the agent signs in to every site itself.
  On the **first** `meiva start` on a browser-capable host, when the profile is
  still absent **and** a human is at the TTY, converge's `_ensure_browser`
  (`services/desktop/browser/profile.py:ensure_browser_profile`) offers to seed it by
  **copying your daily Chrome profile** (macOS `~/Library/Application Support/Google/Chrome`,
  Linux `~/.config/google-chrome`) into `$MEIVA_HOME/chrome-profile/` instead. Copying
  hands the agent your full logged-in identity (cookies, sessions, saved
  passwords, signed-in accounts) so it acts as you without a re-login — a security
  trade-off, so it is opt-in behind an explicit confirmation and the default is a
  fresh profile. The copy excludes lock/socket files (`Singleton*`) and
  regenerable caches, reports its size first, and refuses while Chrome is still
  running (copying live SQLite risks a corrupt import — quit Chrome and retry).
  **Guardrails**: any existing profile directory is never touched, including an
  empty or partial first copy (idempotent across restarts; prod's multi-GB logged-in
  profile survives every start); non-interactive
  paths (root revival, boot autostart, a `cli.fleet_update` start) never prompt and
  always take the fresh default; a host with no daily Chrome degrades silently to
  fresh.
- **Capability and readiness:** consult the [browser gating owner](../../../docs/agent_runner_side/browser/browser/gating.meiva.okf.md).
  `meiva status` surfaces skipped capability reasons. On macOS, a waiting session
  needs the service account's active GUI login and readable login Keychain;
  resolve the reported prerequisite instead of unlocking or editing the profile
  through an agent. Set `MEIVA_BROWSER_ENABLED=false` when choosing to opt out.
- **Service-owned — don't start Chrome by hand**: the `meiva-browser`
  session is the single owner of the CDP port and `$MEIVA_HOME/chrome-profile/`. A
  manually-launched Chrome on that profile takes the singleton profile lock, so
  the daemon's Chrome forwards-then-exits and the session dies — `meiva
  status` cannot infer service ownership from a successful `/json/version`
  alone. The browser probe binds the listener to this home's profile and process
  identity; see the shared browser owner above for orphan reconciliation. The daemon guards the collision: `main()` probes the CDP
  port first and refuses with a clear message rather than exec'ing a second
  Chrome into the lock. To (re)take service ownership, stop the squatter, then
  `meiva start` (or let the next root health round revive the session once the port is
  free) — and the refusal message now names that remedy itself. When the squatter
  is one of *ours* (a Chrome left outside the session by a `SingletonLock`
  handoff), `meiva stop --force` sweeps it, so there is no pid hunt: it kills
  every Chrome running on this cluster's `--user-data-dir`. A Chrome on some other
  profile is deliberately left alone — it cannot be positively identified as ours,
  and the operator's own browser is the thing that must never be killed — so that
  one is still quit by hand.
- **Browser lifetime**: restart preserves the running `meiva-browser` session; a
  cluster update (`cli.fleet_update down`) keeps it while the window's changes do
  not reach the browser code — the
  [keep decision](../../../../docs/decisions/runtime/updates/release/2026-10-17-component-scoped-keep-on-update.md)
  adds `--keep-service browser --keep-service browser-mcp` — and restarts Chrome
  together with the MCP upstream when they do (`--restart-all` forces that).
  Default `meiva stop` closes it; `--keep-service browser` retains it. The profile and its logins survive either choice. `meiva start`
  reuses a retained session and relaunches a stopped one. Explicit force stop
  and cluster destroy additionally sweep owned Chrome processes outside the
  recorded session tree (`services/desktop/browser/orphan.py`).
- **First login is the user's job**: the headed window opens on the host's
  desktop; **you** sign into the target sites (e.g. Google / Xiaohongshu) once — the
  agent does not (and cannot) log in for you. The dedicated profile persists the
  session across restarts.
- **Profile isolation**: `$MEIVA_HOME/chrome-profile/` is separate from your daily
  Chrome profile (isolated cookie jar; signing it into Google does not evict your
  daily profile's sessions). It holds real logins — any agent on the host acts as
  those identities, so log in only what is needed; use a separate account for
  hardest isolation.
- **Display**: needs a real display (fine on a macOS desktop host); the Chrome
  window is visible on that host's screen.
- **Verification is manual**: live-browser use (driving a real site) is checked by
  hand, like the other real-MCP-server integrations — not in CI.
