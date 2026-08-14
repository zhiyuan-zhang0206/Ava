# Prompt injection — boundary map + what is (and is not) built

> **Status: a content-layer scanner IS built and on by default. The structural
> boundary is still deferred.** The 2026-06-07 header on this doc said "deliberately
> not built" and stayed stale through the 2026-06-30 implementation — it was never
> updated. Corrected here.
>
> **Built** — `ava/security.py` (~240 lines), a rule-based scanner gated on
> `AVA_SECURITY_SCAN_ENABLED` (`shared/config/agent.py`), **default on**. It is
> wired into every one of these ingestion points:
>
> | Call site | Source tag |
> |---|---|
> | `ava/files.py` — `ava.files.read` | `file.read:<path>` |
> | `ava/web.py` — `web.search` results, `web.fetch` answers | `web.search` / `web.fetch` |
> | `ava/mcps.py` — **every MCP tool return** | `mcps.<server>.<tool>` |
> | `agent/graph/_chat_inbound.py` — **inbound chat** | `inbound.chat:<source>` |
> | `ava_builtins/plugins/ava_code/plugin.py` — the `AGENTS.md` auto-injection | `context-file:<path>` |
>
> Design points worth keeping: a hit **does not mutate the content** (no marker
> prepended, so the scan is trivially idempotent and never corrupts what the agent
> reads) — findings are buffered in-memory during the exec turn and surface as a
> SECURITY system note in the same exec's messages delta, injected by the exec
> node (`agent/graph/_exec.py`) after the exec-result ToolMessage; there is no
> side-channel file (user ruling 2026-08-11). Memory writes have their own guard
> in `ava/files.py`, stamping `injection-risk: flagged` on a note whose body
> carries already-flagged content, which is candidate defense #3 below in its
> cheap form.
>
> **Not built** — everything structural: the sandboxed deprivileged reader, egress
> allowlisting, privilege separation. (On-install skill scanning **is** built —
> `shared/skill_scan.py` refuses a third-party skill package carrying critical
> supply-chain patterns; see
> [`skill-supply-chain-trust.md`](skill-supply-chain-trust.md).) Those remain
> deferred for the reasons this doc lays out, and they are the ones that would
> actually *close* the hole rather than lower the rate.
>
> **Two real gaps in the built coverage** (see "Coverage gaps" at the end).

## Why this is categorically different from the auth question

The gateway-auth threat is **bounded** (no actor exists outside the single-user
trust boundary — see [`non-goals.md`](../../conventions/non-goals.md) "Auth / multi-user"). Prompt
injection is **unbounded**: untrusted content flows straight into the most
powerful actor in the system. The agent holds `execute_code` (raw Python = bash +
the full SDK); the moment attacker-controlled text enters its context and hijacks
it, it can do anything the agent can — exfiltrate via web/channels, write files,
`spawn`/`terminate` peers, trigger a cluster update, poison memory. The attacker never needs
to be on the network; they only need the agent to *read* their content.

## Ingestion surface — where untrusted content enters

- `ava.web.fetch` / `ava.web.search`, `ava.understand(url)`, `ava.mcps.chrome.*`
  (a live browser) — arbitrary web content.
- `ava.files.read` + **`ava_code`'s AGENTS.md auto-injection** — cloning a
  malicious repo auto-walks its `AGENTS.md` into the prompt: direct injection the
  agent never "chose" to read.
- The content-feed skills (x / youtube / zhihu / xiaohongshu / douyin) —
  untrusted social content by definition.
- A peer's `send_message` — an already-injected agent injects its peers/parent.
- **Memory recall — the nastiest.** A poisoned note is *durable*,
  *cross-machine*, and *auto-recalled*: one injection re-fires for sessions.
- **Skill install** — a third-party skill is code + instructions; installing one
  from an untrusted source is direct code execution, not just content.

## The boundary truth

1. **Content-layer defenses are mitigation, not a boundary.** Delimiters ("treat
   as data, never instructions"), classifiers scanning ingested content, and even
   RL-training the model to resist injection all *lower the rate* — they do not
   close it. Anthropic's own stance: "no browser agent is immune… a 1% attack
   success rate still represents meaningful risk." OpenClaw write-ups call the
   un-structured case "the security boundary that doesn't exist."
2. **The only real boundary is structural** — OS-level sandbox (container /
   gvisor / firecracker / seccomp + separate UID) + network **egress allowlist** +
   privilege separation. And even this **leaks**: Claude Code's sandbox shipped
   repeated bypasses (SOCKS5 null-byte across ~130 versions, `/proc` reads,
   network-allowlist bypass). Structural = defense-in-depth, not a guarantee.
3. **Ava-specific: there is no hard boundary today, by two design choices.**
   - Ava is **autonomous — no per-action human-in-the-loop**. Claude Code's
     *primary* everyday defense is a permission prompt before sensitive ops; Ava
     deliberately doesn't have it.
   - `execute_code` is **raw Python**, so `AVA_SDK_DISABLE` / capability scoping
     is **not** a security boundary — a hijacked agent just `import subprocess`.
     (This is the same point `non-goals.md` "Agent permission verification"
     already makes: the real boundary needs a sandbox.) Reader/actor privilege
     separation only becomes a *real* boundary once the reader is OS-sandboxed;
     without that it still has bash.

## What the field actually ships (so we don't reinvent later)

- **Claude Code** — layered: (1) permission system (human approves sensitive
  ops); (2) classifiers scanning untrusted content (hidden text, manipulated
  images, deceptive UI); (3) safe-command allowlisting; (4) a sandbox runtime
  (beta) with kernel-level fs + network isolation, egress via a proxy with a
  domain allowlist + prompt-on-new-domain; (5) RL training. Explicitly "not
  solved."
- **OpenClaw** — (1) XML delimiters + "data only" directive; (2) **reader/actor
  privilege separation** (a limited-tool reader summarizes raw content; an actor
  with action tools reads only the summary, never the raw content) — there is an
  arXiv paper specifically on this as OpenClaw's structural defense; (3) sandbox
  with only provisioned credentials ("attacker has a sandbox, not your machine");
  (4) **an on-install skill-poisoning scanner** that vets every skill at install
  time.

## Candidate defenses at common entry points (on the shelf, not building now)

Ranked by leverage for Ava's actual shape. None built; listed so a future
trigger has a starting point rather than a blank page.

1. **On-install skill scan** (à la OpenClaw) — vet a skill's text for injection /
   exfil patterns when it enters via `ava plugins install`. Cheapest high-value
   move *if* untrusted skills ever become a thing.
2. **Sandboxed, deprivileged, disposable reader** for untrusted ingestion (web /
   feeds / chrome): no host fs, no secrets in env, egress allowlisted, no
   cluster-update / channels / spawn; returns extracted data as an artifact to the
   trusted layer. This is the structural answer — but it needs the OS sandbox
   (the deferred non-goal) to be real, since reader tool-restriction alone is
   bypassable.
3. **Memory-write gating** — keep untrusted-ingesting context from committing
   durable memory; only the trusted layer writes after vetting (kills the
   persistent-injection vector).
4. **AGENTS.md auto-injection from trusted repos only** — gate the auto-walk so a
   freshly-cloned untrusted repo's `AGENTS.md` isn't silently injected.

### What "we deprioritized content-layer defenses" turned out to mean

The original text here ranked content-layer work outside the priority list, on the
grounds that the model is already RL-trained to resist injection so a prompt
directive is marginal. That judgment stands **for the kind of content-layer defense
it was actually about** — and that kind is still not built:

- ❌ **Not built, still deliberately not:** delimiters / escaping / "treat the
  following as data, never instructions" wording in the system prompt. Marginal
  against an RL-trained model, and it cuts against the prompt-minimalism rule.

What *was* built is a different thing the original list did not anticipate:

- ✅ **Built:** a rule-based **scanner** at the ingestion boundary that flags
  suspicious content, reports it out-of-band as a system note, and marks
  memory writes derived from flagged content. It changes nothing about the
  prompt and does not touch the content itself — it is detection + provenance,
  not persuasion.

The distinction matters because the two get conflated under "content layer": one
tries to *instruct the model* out of being injected (rejected); the other *tells the
operator and the memory pool* that something looked wrong (shipped). Neither is a
boundary — point 1 of "The boundary truth" is unchanged.

## Triggers — when to actually build

- Installing skills / plugins from **untrusted or third-party** sources (today
  everything is the user's own / trusted) → build the on-install scan + reader
  sandbox first.
- Processing **attacker-chosen arbitrary URLs at scale** (vs the curated feed
  accounts the user follows).
- Going **multi-user** or exposing anything publicly (couples with the auth
  trigger).
- **Any real incident** — a single confirmed injection flips the cost/benefit.

## Coverage gaps in the scanner that IS built

Two ingestion points from the surface map above have **zero** `scan_content` calls,
and both are load-bearing:

1. **The content-source skills** (`web-sources` — rss / youtube / generic —
   plus `web_media`, and the x / zhihu / xiaohongshu / douyin family). These are
   *by definition* untrusted third-party content, and they are the one category the
   surface map calls out as such. They fetch through their own scripts rather than
   `ava.web.*`, so they bypass the scanned path entirely. This is the widest gap.
2. **`ava.understand(url)`** (`ava/_understand.py`) — listed in the ingestion surface
   below as an arbitrary-web-content entry point, but it does not import
   `ava.security` and is not in the call-site table above.

Both are cheap to close (they are call-site additions, not new machinery), and
closing them is what makes the built scanner's coverage claim honest.

## Open research (the "how broad is the surface" question)

Before building anything, measure rather than guess: which ingestion entry points
actually carry untrusted content in real use, how often, and into which agents
(do the privileged orchestrators ever ingest raw untrusted content directly, or
is it already mostly funnelled through fetch/feed paths that could be isolated
cheaply?). The answer decides whether #1–#4 above are even needed, and in what
order. This ties into the [sandbox non-goal](../../conventions/non-goals.md) whose
trigger ("agent runs untrusted third-party input") is arguably already met by
`web.fetch` / feeds / chrome — the gap to size is "met in principle" vs "met at a
volume/exposure that warrants the build."
