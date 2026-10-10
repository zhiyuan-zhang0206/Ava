---
type: doc
title: "Browser MCP process composition"
description: "The shared MCP entry owns its captured image, configuration readers and lazy logging database without adding a business database startup gate."
tags:
- services
- browser
---

# Browser MCP process composition

`services.desktop.browser.mcp_daemon.main` captures one `LoadedCommit` before
booting its existing `ConfigBoot`. Logging receives that same image; the
entry's `CodeVersion` resolves only its captured SHA, and its `ProcessDbGate`
supplies the application identity for the logging database. An unknown image
remains unknown and cannot acquire a version from a later checkout.

The entry builds its logging pipeline with an explicit database factory.
Each factory call reads `db_config_from_boot(config)` and supplies the same
gate. Machine identity and upstream timeout readers also retain this same
configuration owner. Configuration changes therefore reach the next logging
dial or upstream connection rather than a mirrored settings object.

Chrome and MCP requests use CDP and the Unix socket; both service specs retain
`requires_db=False`. Pipeline construction does not dial Postgres or resolve
the captured version. Its drain thread dials lazily, keeps the existing JSONL
mirror and backs off on an unavailable database. A DB outage or schema mismatch
does not become a new startup admission gate, and the watchdog keeps reviving
these services through a DB-scoped round block. The lock-free Unix `ping`,
shared page registry, upstream reconnect loop and bounded shutdown order are
unchanged.
