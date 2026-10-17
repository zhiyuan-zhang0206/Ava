---
type: doc
title: scripts/content_lint/docs/panel-jargon — User-Facing Panel Gate
description: The lint_panel_jargon.py gate — body findings, the configurable rule file, budgets and exit status for a workspace to_user.md panel.
tags:
- scripts
- lint
---

# scripts/content_lint/docs/panel-jargon — User-Facing Panel Gate

A panel such as a workspace `to_user.md` is read by the user, who has no context
about internal identifiers: an explanation that is accurate but written in
codenames does not let the reader catch up. The gate splits the document at the
first evidence heading (identifiers, paths and links belong there) and reports
body findings for a rule set that lives entirely in
`panel_jargon_rules.toml`: bare change/task numbers and issue numbers are hard
rules, while paths, batch codewords, unglossed acronyms and the glossary length
are counted against the configured budget. It never edits a panel.

The body of a panel — everything before the first evidence heading — carries no bare internal identifier, issue number or batch codeword: those are hard findings that exit non-zero. Soft findings (repository paths, unglossed acronyms) and the number of glossary entries are bounded by the same rule file, and exceeding either budget also exits non-zero. Identifiers inside the evidence area are never reported, the rule file is the only place patterns and budgets live, and the gate is read-only.

Parent: [[scripts/content_lint/docs/content_lint.meiva.okf.md]].
