# CLI full-settings profile

## Context

Launchers mark gateway, agent, and runner children with
`AVA_PROCESS_PROFILE`, but a CLI started from an agent shell inherits that
marker. The agent profile deliberately omits the `alerts` domain, causing CLI
LGTM work that reads `settings.alerts` to fail.

## Decision

`cli.main.main()` clears the inherited marker before every early-return path.
The CLI is a full-settings process, matching the no-marker rule in
`shared/config/profiles.py`; it does not become a member of any service
profile and the profile consumption matrix remains unchanged.

The environment key remains literal in `cli.main`: importing
`shared.config.profiles` would initialize `shared.config` before `main()` can
clear the marker.
