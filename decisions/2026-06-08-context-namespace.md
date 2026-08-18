# Context namespaces: the (description, body) model

## Context

An agent's context is scarce. Everything Ava exposes — skills, commands, MCP
tools, the SDK surface — competes for it. A flat catalog of capabilities means
every capability's description is resident in context at once; the surface grows,
and the agent pays for the whole plane even to find the one thing it needs.

Two organizing axes collide on the same surface:

- **Capability type** — skills, `/`-commands, MCP tools.
- **Provenance** — which group a capability came from. Capabilities can be
  bundled and vendored from external packaging units, so the same surface mixes
  in-repo and bundled capabilities with no marker of origin.

The forcing question: how does provenance fold into the same context economy as
type, without making the agent reason about a packaging unit it can only
half-see?

## Decision

Model **everything the agent can reach as a (description, body) pair**:

- **description** — cheap, scanned at the upper level to decide *whether* to
  reach for the thing (frontmatter, a one-line summary, a docstring's first line).
- **body** — expensive, loaded only once chosen (the skill body, the command
  template, the full module, the actual tool call).

A **namespace is the structure that folds the tree**: the upper level shows one
folded line per node; you pay for a body only when you descend. For a
context-scarce agent, nesting is not a readability cost — it *is* the lazy-loading
mechanism. This inverts "flat is better than nested" (which assumes a human reads
the whole plane at once).

Concretely:

- **Folder tree = namespace tree.** A directory tree under a mount point *is* the
  namespace tree, to any depth. A `SKILL.md` is a leaf; the folders above it are
  its namespace. The repo's skills mount at the root (top-level skills stay bare);
  a plugin's skills mount under the plugin's name. So `skills/coding/tdd/` →
  `ava.skills.coding.tdd`, and a vendored plugin's skills nest under its name.
  Two renderings of one location: a `.`-joined identifier for `/`-commands and
  listings, and a `.`-joined attribute path for Python access.
- **The agent sees a skill tree, not plugins.** Provenance surfaces as *location
  in the tree*, never as an introspectable "plugin." A plugin is an install/dev
  packaging unit (it also bundles hooks and SDK wrappers, framework-layer and
  invisible to the agent); it merely *mounts its skills into the tree under its
  own name*. The agent's unit is always the capability.
- **Namespaces-first listing; folders as packages.** Browsing lists each
  top-level node — bare skills and namespaces alike — by its one-line description;
  the agent descends a namespace only when relevant. A folder behaves like a
  Python package — both a thing and a container:
  - **leaf** — `SKILL.md`, no skill-bearing children: a plain skill.
  - **root skill** — `SKILL.md` *and* children: invocable *and* a namespace;
    rendered as its own body followed by its children's listing.
  - **labelled namespace** — `INDEX.md`, no `SKILL.md`: a namespace carrying an
    authored description. Optional — a bare folder synthesizes a `contains: …`
    line, so nothing forces an `INDEX.md`.
- **Commands become addressable prompt functions.** A command is a named,
  discoverable, described intent that one agent can send another — not just a
  human's composer macro. It rides the existing inbound path and command
  expansion: sending reuses ordinary message-send with a `/name args` body, and
  command expansion is source-neutral (the sender is already attributed
  upstream). A read-only catalog lists the sendable commands (name + description +
  instruction-hint, no body).
- **MCP stays flat.** MCP already carries its own two levels (server → tool),
  server names are globally unique, and a server can be shared across plugins.
  Its provenance is genuinely different; forcing it under a `plugin:` nesting
  would distort it. Servers stay listed flat by server name; a plugin's
  description may *reference* the servers it bundles.

## Alternatives rejected

- **Flat capability catalog.** Easiest for a human reading the whole plane, but
  it keeps every description resident in context at once — exactly the cost the
  agent can't afford. A family of N near-identical adapters should collapse to one
  namespace line until needed.
- **Expose the plugin as an agent-visible thing.** A plugin bundles
  framework-layer pieces (hooks, SDK wrappers) the agent can't see, so asking it
  to introspect "a plugin" exposes something only half-visible. Provenance had to
  surface as *namespace*, leaving "plugin" an install/dev concept the agent never
  names.
- **A new `send_command` primitive for command-as-message.** A second
  delivery channel parallel to message-send, and a near-clone of agent spawn. The
  whole value is *named + discoverable + described*; reusing the existing inbound
  path and message-send keeps that value without a second spawn primitive or a new
  channel.
- **Nest MCP under `plugin:` for uniformity.** Symmetric with skills/commands,
  but MCP's two-level server→tool structure and globally-unique, cross-plugin
  server names make plugin-nesting a distortion rather than a fold.
- **A required `INDEX.md` per namespace (with a linter).** A bare namespace
  synthesizing a `contains: …` line is enough; an undocumented module is legal,
  just terser. No required-field invariant.
- **A sibling `using-X` orientation leaf for a folder's entry doc.** A folder
  that is both a skill and a namespace puts its entry and its catalog at the same
  node — cleaner than a separate orientation sibling.

## Consequences

- The context economy is the payoff: the agent reads one folded line per
  top-level node and pays a body only on descent. Capability families cost one
  line until opened.
- Two axes (type, provenance) reduce to one tree shaped by folders. There is no
  separate "plugin" concept in the agent's mental model — moving or vendoring a
  capability is a folder move, and internal cross-references stay within the
  subtree.
- One identifier carries two renderings (`:` for commands/listings, `.` for
  Python). Both must stay derivable from the same folder location.
- Commands and skills converge: a command is an addressable, described prompt
  function reachable by both human and agent over the same inbound path. The
  standing boundary is to keep its named/described value without re-cloning agent
  spawn.
- MCP is deliberately *not* unified into the folder tree; its provenance is
  referenced by description, not nested. Accepting this asymmetry is the price of
  not distorting MCP's native server→tool shape.
