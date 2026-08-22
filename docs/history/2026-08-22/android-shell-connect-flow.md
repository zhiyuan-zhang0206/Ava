# Android shell connection flow

Implemented the approved single-field connection flow for the shell. The
primary address now identifies the cluster console and derives the default
gateway, rather than asking first-time users to coordinate two ports. An
advanced gateway override remains for installations with a deliberate custom
API endpoint, preserving the existing manual configuration escape hatch.

The Android cluster secret stays outside both the webview and `settings.json`:
native Rust exchanges it for the session cookie, then the Kotlin overlay saves
it as AES-GCM ciphertext protected by Android Keystore only after that exchange
succeeds. A rejected stored secret deliberately remains in place and falls back
to the console login, so a transient or mistaken attempt cannot silently erase
the user’s credential.

Connection readiness is an HTTP probe of the console rather than a bare TCP
connect. This distinguishes a live console, a normal signed-out response, a
failed HTTP service, and a rollout window. Rebuilding the webview is deferred
until after the settings IPC reply so Android can settle the submit promise.
