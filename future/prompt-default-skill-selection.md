# Default skill exposure review

Status: reviewed candidates; no package enable/disable or deployment change.

## Three separate decisions

Keep full-body preloading empty for general agents. Catalog exposure advertises
capability; a matching task loads the body. Installation determines availability
and must not be changed merely to shorten a prompt. User-installed entry skills
need discovery even when absent from a built-in shortlist.

The first implementation folds nested guides under real entry skills, retaining
orphan leaves and explicit agent lists. It does not impose a total truncation cap
or turn a general-agent shortlist into a capability boundary.

## General-agent shortlist

| Entry | Decision | Reason |
|---|---|---|
| ava-workflow | Default strategy entry | Chooses alignment, goals, coordination and evaluation without mandatory phases. |
| ava-guide | Default Ava entry | Routes operations, agent management, packages, publishing and configuration. |
| ava-serious-engineering | Domain entry | Covers consequential software work; individual practices load through the root. |
| ava-serious-research | Domain entry | Covers research evidence and evaluation; individual practices load through the root. |
| web-sources | Installed capability entry | Makes web retrieval discoverable without advertising each source adapter permanently. |
| web-ai | Installed capability entry | Distinct browser-backed model capability; not a substitute for web retrieval. |
| audio-transcribe | Installed capability entry | Clear media-specific trigger and dependency requirements. |
| gmail | Installed capability entry | Mail capability must be discoverable; installation is not authority to send. |
| sms | Installed capability entry | Host-specific capability; expose only where actually available. |
| telegram-send-file | Installed capability entry | Distinct file delivery capability; not a standing reporting channel. |
| ava-fleet | Enabled-plugin entry | Optional collaboration implementation; not a default topology. |
| ava-memory | Enabled-plugin entry | Memory operations and stewardship on relevant tasks. |

None of these requires universal full-body preloading. The existing per-agent
catalog list can express a specialist subset; missing host capabilities should
not be advertised by a fixed cross-machine allowlist.

## Selected methods and specialist work

| Entry | Preferred discovery | Boundary |
|---|---|---|
| ava-goal | Workflow method selection | Sustained outcome pursuit, not every task. |
| ava-dynamic-workflow | Workflow method selection | Executable orchestration, not every parallel task. |
| ava-being-a-long-running-agent | Workflow continuation selection | Waiting and recovery detail, not universal standing work. |
| ava-deep-research | Research/capability selection | Multi-source investigation; distinguish from doing an ML research project. |
| skill-creator | Guide package/modification routing | Skill authoring tasks. |
| ava-corp | Explicit organization task | Do not assign every cluster a corporate hierarchy. |
| ava-self-evolution | Explicit improvement task or assigned service | Do not start a standing mining job from ordinary work. |
| sweeper | Explicit maintenance task | Do not launch a general debt sweep from every bug fix. |

Before hiding these independent entry skills from the wildcard directory, verify
that each routing entry links to it and that a held-out task discovers it. The
current change keeps them discoverable; this table is the concrete review list
for a narrower general profile, not an implicit uninstall instruction.

The preview also contained ava-ui, ava-watcher and ava-ultra-speed entries absent
from the current source skill catalog. Check their installed provenance before
calling them current built-in defaults. Package cleanup is separate from prompt
rendering and was not performed here.

## Acceptance for a narrower default

Exercise ambiguous alignment, verification-heavy repair, reusable orchestration,
ordinary direct work, and a previously unseen installed capability. Observe
actual skill loads, relevance, completion evidence, SDK errors and USD cost.
Shorter text alone does not establish better selection. Compare the same task
set before changing a cluster default, preserving source and config identity.
