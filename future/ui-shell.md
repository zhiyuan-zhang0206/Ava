# UI shell — remaining distribution work

The cross-platform shell itself lives in `ui/app/` and is current-state
documented in [`app.ava.okf.md`](../ui/app/app.ava.okf.md). The old Electron
package is gone. This file tracks work that cannot be completed by source code
alone.

## Signing and store distribution

- Provision Apple Developer ID/notarization credentials, a Windows code-signing
  certificate, and an Android upload keystore as GitHub secrets. Tag releases
  fail closed until every group exists; manual dispatch keeps unsigned
  validation builds available without publishing them.
- Exercise a real `shell-v*` tag with every signing group present, then verify
  installation and updater replacement on clean macOS and Windows machines.
- Decide whether the Android APK remains a direct GitHub Release download or is
  distributed through a store. Android's `specialUse` foreground-service type
  requires an explicit use-case declaration during store review.

## Android closed-app notifications

The current event bridge deliberately reuses the authenticated webview's
`/api/system` SSE stream. A foreground service keeps that process resident, but
force-stop/closed-app delivery is impossible without a push path. If that user
experience becomes required, design it as a separate gateway-to-push-provider
system (device registration, credential rotation, payload privacy, opt-out),
not as a larger retry loop in the shell.

## Network hardening

Android network-security configuration accepts domain names but not RFC1918 or
tailnet CIDR ranges. The current shell therefore checks every resolved address
before persisting a cleartext endpoint. A stronger future boundary would pin a
private TLS origin or enforce destination IPs in a native request layer; either
choice needs a certificate/provisioning design for private clusters.
