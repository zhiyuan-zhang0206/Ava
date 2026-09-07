---
type: doc
title: "Issue Intake"
description: "Issue templates and label-based routing — the GitHub side of filing and triage (bug -> QA, feature request -> PM, UX feedback -> P0 lead)."
tags:
  - ci
  - ops
---

# Issue Intake

Three issue forms (`ISSUE_TEMPLATE/bug_report.yml`, `feature_request.yml`,
`ux_feedback.yml`) with `config.yml` disabling blank issues — every filed
issue carries exactly one template label (`bug` / `enhancement` / `ux`).

`workflows/issue-router.yml` reacts to `issues:opened` and routes by that
label: `bug` -> `route/qa` (Ava QA line, #3242), `enhancement` -> `route/pm`
(Ava PM, #3187), `ux` -> `route/p0` (Ava P0 lead, #405), adding the routing
label and a pointer comment via `gh api` (GITHUB_TOKEN, issues:write only).

GitHub Actions cannot reach the cluster's tailnet gateway, so agent wake-up
is a later gateway-side watcher step consuming these routing labels — the
GitHub side is the pre-provisioned intake surface (D1 batch).

## Key dependencies

- [[.github.ava.okf.md]] — parent overview and the workflow list.
