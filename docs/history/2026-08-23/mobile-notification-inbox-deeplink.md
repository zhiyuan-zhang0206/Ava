# Android notification Inbox deep link

Android local-notification taps now route the console to the fleet Inbox at
`/fleet#inbox`. The Tauri notification plugin's JavaScript tap event is not a
reliable cold-start signal, so `AvaClickPlugin` captures its launch intent in a
one-shot in-memory mailbox instead.

The notification bridge reads that mailbox only after the credential-gated
system SSE has opened. This preserves a tap through an auto-login reload and
keeps navigation in the existing console window; a consumed marker cannot
redirect a later page load.
