---
type: doc
title: Browser Tab Groups
description: Every page an agent's browser call creates is filed into one Chrome tab group per agent, through an extension the browser-mcp daemon loads over CDP.
tags:
- browser
---

# Browser Tab Groups

The shared Chrome shows one tab group per agent, titled `Meiva #<id>` (ids are unique, labels are not). It holds every page that agent's browser calls **created**: an explicit `new_page` and the page a page-less first `navigate_page` auto-creates. A page the agent merely selected (a user's tab) is never grouped, nor is a page opened in an isolated browser context (`isolatedContext`): extensions cannot see such tabs. The title carries no TTL and no color; Chrome assigns the color.

## Mechanism

- **No CDP API.** Chrome's DevTools protocol has no tab-group commands, so `services/desktop/browser/tab_groups/extension/` ships a small MV3 extension (permissions `tabs`, `tabGroups`, `debugger`). Its service worker exposes `meivaGroup(targetId, title)`: map the CDP target to a tab (`chrome.debugger.getTargets()`, no attach, no debugger bar), find open groups by title (coalescing any duplicate open groups with that title into the first one), else create the group and set the title. It keeps no state, so daemon restarts and Chrome restarts need no mapping repair.
- **Loaded when needed.** `Extensions.loadUnpacked` works on the ordinary `--remote-debugging-port` connection (no launch flag), the load lasts only for that Chrome session, and the worker stops about 30 seconds after its last event. `tab_groups/grouping.py` therefore asks `Extensions.getExtensions` and the live targets on each grouping: a running worker of the shipped file name is used as is; otherwise (Chrome restarted, worker idled out, older worker file) it issues the idempotent load and waits for the worker. A live worker is never reloaded, because the reload tears it down under an attached call. Nothing is remembered between calls, so daemon restarts need no repair.
- **The worker file name carries a content hash** (`group-<sha256[:12]>.js`, enforced by a test). Chrome caches an extension's worker script in the profile and keeps running the cached copy when only the content or the manifest version changes; a new file name is the only reliable update. Edit the worker, rename it, update `manifest.json`.
- **Page id to target id.** The daemon's page ids are chrome-devtools-mcp sequence numbers, not CDP targets. `ChromeMcpDaemon._new_page` runs under the machine-wide serial lock: `AgentTabGroups.snapshot()` lists Chrome's page targets (`/json/list`) before the upstream `new_page`, and `group_new_page()` lists them again after and takes the one new target. Any other count (a user opened a tab meanwhile, a popup) is reported and left ungrouped; it never guesses. Those two listings (2 s timeout each) are all that runs under the daemon lock.
- **Grouping runs off the lock.** Loading the extension, attaching and calling the worker happen in a background task (10 s timeout) started by `group_new_page()`, so a slow or wedged Chrome never delays a page creation or another agent's call; the page is grouped moments after `new_page` returns. Tasks run one at a time under the grouping's own lock, because two groupings of one agent both look the group up by title and would each create it. `cancel_pending()` drops them when the daemon stops (an upstream death ends the process).
- **The extension ships in the package.** Hatch packages the `services` tree whole, so the unpacked extension directory is installed beside the code.

## Removal is Chrome's, not ours

Nothing deletes a group. Chrome removes a tab group, and its saved-group record, when the group's last tab closes. Every path that closes an agent's pages goes through the upstream `close_page`: `release_agent_page` (called on hosted termination via `agent/ownership/hosted_cleanup.py`, closing every page the agent created) and the hard-TTL sweep (see `page_lifecycle.py`), and tests assert each one. An agent's group therefore disappears with its last page; the hard TTL bounds how long any page, and so any group, can live. Chrome leaves one orphaned saved-tab record per group in the profile; it is not visible.

## Failure policy

Grouping is presentation only. A failed load, CDP error, missing worker, ambiguous target or wedged Chrome logs a WARNING (with the cause) and the page operation proceeds untouched: `new_page` and `navigate_page` never fail because of grouping. This is a bounded exception to fail-fast; see [the decision](../../../../../docs/decisions/engineering/design/contracts/2026-10-12-presentation-only-failure-degrades-with-warning.md).

Parent: [[services/docs/agent_runner_side/browser/browser/browser.meiva.okf.md]]
