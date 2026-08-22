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

## Update

The follow-up review established that Android mobile-plugin calls made from the
main looper deadlock waiting for the JNI response that same looper must deliver.
The shell now uses asynchronous native plugin calls or the blocking executor,
and mirrors the Keystore result in transient process state for startup login and
the prelude. Desktop keeps its existing arbitrary worktree entry and gateway
ports; the strict console/gateway normalization applies only to Android.

## Further update

Android wry 0.55 can wedge the main-pipe IPC after a destroy-and-rebuild during
the post-connect response burst. Android therefore refreshes the existing
webview prelude and navigates it only after the settings response flushes;
desktop retains its rebuild behavior. The Android overlay also carries ProGuard
rules that preserve JNI-named Tauri plugin classes and `@Command` metadata in
minified release builds.
