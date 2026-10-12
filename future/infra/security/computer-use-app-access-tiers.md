# Computer Use — per-app access tiers

> **Status: deferred by the maintainer on 2026-10-12; not scheduled.** Nothing
> here is built. This page records the problem, the prior art and the open
> questions so the idea is not rediscovered from scratch. It makes no decision.

## Problem

Computer Use has no per-application authorization layer at all. The current
safety surface is [`computer-mcp.ava.okf.md`](../../../services/docs/agent_runner_side/computer-mcp/computer-mcp.ava.okf.md):
a platform-capability gate (the signed helper must be enabled and capable), a
single-holder screen lease, one `computer_action` audit event per call, the
top-left-corner [fail-safe](../../../services/docs/agent_runner_side/computer-mcp/native-input.ava.okf.md)
and the system capture badge on the operated window
([observation frames](../../../services/docs/agent_runner_side/computer-mcp/observation-frames.ava.okf.md)).
The same node records that the whitelist, quota and denied-app gates were
removed on 2026-08-10, leaving "permission division" as a peer convention.
The comment on `frontmostApp` in
[`helper/main.swift`](../../../services/desktop/permissions_helper/helper/main.swift)
still describes a denied-app keyword gate that no longer exists in the Python
daemon (`services/desktop/computer/`), so it is stale rather than a second layer.

What is missing is a way to say, per application, "the agent may look", "the
agent may click and scroll" or "the agent may type and press shortcuts". Today
every application is full control, including terminals, password managers,
system settings and trading or payment apps, for every agent on the machine.

## Prior art

- **Claude desktop computer use** asks before each application and grants it for
  the session. The control level is fixed by app category and the user cannot
  change it: *View only* (browsers, trading platforms: screenshots only),
  *Click only* (terminals, IDEs: click and scroll, no typing or shortcuts) and
  *Full control* (everything else). Apps with broad reach (terminals, file
  manager, system settings) carry an extra warning in the prompt. A Denied apps
  list rejects apps without prompting. Only one session can control the computer
  at a time. Investment, trading and cryptocurrency apps are blocked by default.
  Sources: [computer use (CLI)](https://code.claude.com/docs/en/computer-use),
  [desktop app](https://code.claude.com/docs/en/desktop),
  [support article](https://support.claude.com/en/articles/14128542).
- **OpenAI Codex computer use** requires approval the first time an application
  is used, with an *Always allow* choice that persists (on Windows as
  `always_allowed_app_ids` in `config.toml`). It cannot automate terminals or
  ChatGPT itself, and cannot authenticate as an administrator or approve system
  permission prompts. Administrators can restrict applications or disable the
  feature with `[features].computer_use = false` in `requirements.toml`.
  Source: [Codex computer use](https://learn.chatgpt.com/docs/computer-use.md).

## Draft shape (a starting point, not a design)

- **Tiers.** `view` (observation only), `click` (pointer: click, drag, move,
  scroll, `focus_app`; no `type`, `key`, and no `ax_act` that sets values) and
  `full`. Observation tools stay available in every tier except a hard deny.
- **Defaults.** A fixed category map as above (browsers and trading or
  crypto apps `view`, terminals and IDEs `click`, remainder `full`), keyed by
  bundle id rather than the display name that `frontmost_app` returns.
- **Checkpoint.** The helper already decides, per input, which window or
  frontmost application the input lands on, for the capture badge
  (`indicatorWindow`, `dispatch` in `helper/main.swift`): the window under the
  point for `click` / `move` / `scroll` / `drag`, the frontmost application for
  `type` / `key`, the pinned PID for `ax_act`. A tier check would reuse that
  decision and sit beside the fail-safe check, so a refused action marks and
  posts nothing. A Python-side check would need its own lookup and race the
  focus.
- **Authorization.** Who approves, where, and how the approval persists are
  open (below). The grant would be data the helper can read, not a prompt the
  model can answer.
- **Relation to existing controls.** A denied-app list is the degenerate tier
  below `view`. The corner fail-safe and the badge are orthogonal and unchanged;
  the tier check is evaluated after the fail-safe, so a stopped action stays
  stopped regardless of tier.

## Open questions

1. Reversal: the 2026-08-10 removal chose peer convention over code-enforced
   governance, and no decision record in `docs/decisions/` gives the reasoning.
   Is this a reversal of that choice, and what did the removed gates cost?
2. Who authorizes: the machine's user at the desktop, the operator through a CLI
   or config, or the agent's owner through the gateway? A prompt on a desktop
   nobody is watching never answers.
3. Persistence: per session, per lease, time-boxed, or stored (config file,
   database)? Where does the store live, and is it per machine or cluster-wide?
4. Granularity: per agent or per machine? Agents share one screen under the
   lease, so a per-agent grant can be wider than what the machine owner meant.
5. Category map: who owns it, is it user-editable (the Claude desktop app fixes
   it deliberately), and how are unknown applications classified?
6. Identity: matching on bundle id, executable path or display name; helper
   binaries and web apps wrapped in browsers escape app-level tiers.
7. Time of check: focus can change between the check and the posted event
   (`type`, `key`). Is a refusal best-effort, or must the helper re-verify?
8. Escalation: a `click`-tier terminal can still be driven by clicking a
   pre-filled command. Is a broad-reach warning enough, or is a hard deny
   (as Codex does for terminals) the right answer?
9. Refusal contract: a refused action should fail fast with a message naming the
   tier and application; what does the audit event record?
10. Whether an `ax_act` that sets a value is `type`-like or `click`-like.
