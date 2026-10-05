---
type: doc
title: gmail skill — Full Gmail Client
description: Read/search/send/reply/forward/draft + newsletter ingestion full Gmail client. Self-contained CLI (pure stdlib imaplib+smtplib+email), App Password from ~/.ava/secrets/gmail-app-password (macOS Keychain fallback); agent calls via bash, reads stdout JSON — not imported into agent namespace, does not drive a browser.
tags:
- extensions
- agent-instruction
---

# gmail skill — Full Gmail Client

## What is it
A complete Gmail mail client (`ava_builtins/skills/integrations/gmail/`), pure IMAP/SMTP, **self-contained CLI**. The key tradeoff it embodies: **not imported into agent namespace, does not drive a browser** — the agent calls `ava_builtins/skills/integrations/gmail/scripts/feed.py` via bash, reads stdout JSON. `feed.py` re-exports the sibling `imap.py` (auth, search/fetch) and `smtp.py` (compose/send/reply/forward, IMAP-APPEND drafts) as one CLI. 11 lenses: search / read / discover / enum / fetch / sync / send / reply / forward / draft / draft-delete. Gmail's `category:`/`list:`/`after:` are transparently forwarded via the `X-GM-RAW` IMAP extension; the full Gmail search syntax is available.

## Authentication Form
Pure stdlib (imaplib+smtplib+email), no third-party SDK. One-time prerequisites: enable two-step verification + generate an App Password, enable IMAP, store the App Password in `~/.ava/secrets/gmail-app-password` (read first) and/or the macOS Keychain (`security add-generic-password -s ava-gmail-imap`, the fallback); the script reads the password file-first and the login from `GMAIL_ACCOUNT` or the entry's acct label. It is one of the 5 skills injected by default into the system prompt index (every agent should know it exists).

The credential model is machine-level, not per-cluster: both the password file and the Keychain entry are scoped to the OS user, not to any `$AVA_HOME`. The password is read from `~/.ava/secrets/gmail-app-password` first (automation must not depend on the macOS Keychain — a locked keychain or pending prompt stalls headless pipelines), falling back to the Keychain entry when the file is missing; the path is deliberately resolved the same machine-level way (never through `resolve_ava_home()`), matching the per-machine `~/.ava/secrets/*.env` convention (`docs/conventions/dev-setup.md`). A dev worktree cluster (home `~/.ava-<dir>`) still authenticates against this same one mailbox.

## Key Dependencies
- [[ava_builtins/skills/docs/skills.ava.okf.md|Skills index]] — full skills catalog
- [[ava/skills/docs/skills.ava.okf.md|Skill System]] — indexed in every agent's `# Capabilities` section like every loaded skill (`skills_to_inject_into_system_prompt` defaults to `*`)
