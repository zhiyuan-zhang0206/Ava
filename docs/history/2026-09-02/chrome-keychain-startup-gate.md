# Chrome Keychain startup gate

## Decision

On macOS, the headed browser daemon waits before its profile initialization and
Chrome launch until the service account owns the active GUI console session, its
`launchctl gui/<uid>` namespace exists, and its login Keychain answers
`security show-keychain-info`. The wait is represented by a short-lived marker
owned by the live daemon; the CDP probe and healthcheck report that state as
degraded and preserve the session for retry. A missing runtime marker falls
back to the same bounded, read-only readiness check, so runtime-directory
failures cannot turn the deliberate wait into restart churn.

Profile repair remains deliberately absent. Automatic setup copies a daily
Chrome profile only into an absent destination. Every existing profile
directory, including an empty or partial one, stays untouched. `Local State`
receives only existence, read-permission, and future-mtime checks with
warning-only output; Ava never reads its contents or writes it.

## Rationale

Chrome's macOS encryption material is held by the login Keychain. A detached
service can pass the static display check while lacking the GUI/Keychain context
needed to decrypt persisted browser state. Launching anyway creates a Chrome
process that cannot safely use the profile's encrypted login data.

The rejected alternative was to start Chrome and rely on the existing watchdog
to restart it after CDP failure. That turns an unavailable GUI or Keychain into
restart churn and repeatedly launches Chrome without its required encryption
material. Explicitly waiting keeps the supervised process stable, makes the
operator-facing state visible, and retries when the user logs in or the
Keychain becomes usable.

## Consequences

- macOS starts triggered through SSH or during boot may wait until a user logs
  in and the login Keychain is available.
- The gate is read-only: it never unlocks a Keychain, parses encrypted Chrome
  state, or resets a browser profile.
- The waiter is not a general browser-health verdict. Once Chrome has launched,
  normal identity and CDP health behavior remains unchanged.
