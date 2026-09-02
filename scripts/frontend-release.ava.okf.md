---
type: doc
title: Prepared standalone frontend
description: CI-only source-absent frontend closure before release activation is permitted.
tags:
- runtime
- frontend
---

# Prepared standalone frontend

The release build opts into Next's standard `output: standalone` with
`AVA_FRONTEND_RELEASE=1`. Ordinary source/development output is unchanged.
`prepare_frontend_release.mjs` private-copies traced server dependencies,
`.next/static`, optional `public`, and the exact Node executable running the
preparer. Input and copied inventories must agree before the result is usable.
Dotenv files, symlinks, hard links and special files are rejected. No dependency
download, build, selection or service lifecycle is performed by preparation.

The exact Node bytes/version/OS/architecture and complete copied files form the
receipt. Node's platform system libraries remain prerequisites; this is not an
OS attestation or a Windows support claim. Only explicitly public build settings
may enter NEXT_PUBLIC variables. Unit secrets/configuration/data never belong in
the image. The current application has no public directory; the proof reports
that absence rather than inventing a production asset.

Dedicated Linux/macOS CI builds with the existing npm lock, retains Node, moves
the source checkout away and serves actual HTTP with the standalone server.
It fetches rendered static resources, checks the copied inventory after serving,
and proves a failed subsequent preparation leaves that server available. The
negative input tests reject changed bytes before writing and dotenv inclusion.

The Python preparer optionally consumes this trusted input inventory, verifies
its private copy before execution and includes all frontend bytes in the same
generation manifest/seal. The existing frontend command and watchdog bind wheel
mode to that loaded generation's retained Node/server, never npm or a moving
selector; source mode keeps its existing behavior. Combined CI invokes the real
wheel command with source absent and fetches HTTP from that command's process.
This does not bypass service-respawn ownership or release admission, which stays
closed until every enabled asset is verified. Native OTel,
enabled plugins, bootable LKG and all-managed-writer closure are separate hard
gates; HTTP alone does not prove full service readiness.

Reference: [Next standalone output](https://nextjs.org/docs/app/api-reference/config/next-config-js/output).
