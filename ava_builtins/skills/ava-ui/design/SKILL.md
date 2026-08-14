---
name: design
description: Visual design system for pages the agent builds — how much design a request warrants, typography, color and neutrals, both themes, layout, copy, and the quality floor. Use when building any web page, HTML output, or UI, before writing markup or CSS.
---

# design — the visual quality bar for agent-built pages

Approach this as the design lead at a small studio known for their versatility,
giving every client a visual identity pitched at the treatment the task actually
calls for. Make deliberate choices about palette, typography, and layout that are
specific to this subject, and avoid templated designs.

Scope: this skill is only about how a page should **look**. Building and serving
it is [`ava-ui`](../SKILL.md)'s job (`ava.ui.serve` / `ava.ui.show`, the widgets
and starters); charts inside it are
[`ava.skills.ava_ui.dataviz`](../dataviz/SKILL.md).

## Read the request first

Calibrate the treatment, not whether to design at all. A doc deserves the same
craft as a landing page — what changes is the treatment that craft is delivered
in.

Many requests call for a **utilitarian** treatment: a plan, a memo, a status
readout, a demo. Make it polished — real typographic hierarchy, considered
spacing, a proper palette — but avoid over-designing. Most pages do not need a
flashy, gigantic hero. Keep flourishes tasteful and limited.

Some requests call for an **editorial** treatment: a landing page, a game, an app
or tool the user will keep or share.

When unsure: a well-composed page is never the wrong answer; an over-designed
visual identity sometimes is.

The fundamentals below apply to everything. The editorial process after that runs
only when the read above says so.

## Fundamentals for every page

**Honor what's already there.** Look for an existing design system first — the
project's `AGENTS.md`, a tokens or theme file, existing component styles. When
one exists, apply it; everything below fills gaps and never overrides. Precedence
is always: the user's own words, then the project's existing system, then your
choices.

**Ground it in the subject.** If the subject isn't already clear, pin it: one
concrete subject, its audience, and the page's single job. The subject's own
world — its materials, instruments, vernacular — is where distinctive choices
come from. Build with real content throughout, never lorem.

**Pair typefaces.** Typography carries the page even when the page isn't about
typography. An agent-served page has no build step and no bundler, and a webfont
CDN is an external dependency that fails silently into a fallback face — so
either commit to a well-chosen system stack or inline the face as a `@font-face`
data URI. Keep running text near 65 characters wide; set a type scale and stay on
it; give headings `text-wrap: balance`, body text room to breathe, and uppercase
labels a touch of letter-spacing.

**Choose neutrals, don't default to them.** A pure mid-grey reads as
unconsidered; a grey with a slight hue bias toward the page's accent reads as
chosen. Pure white and near-black are fine grounds when they suit the subject —
the point is that the neutral was picked, not inherited.

**Design both themes.** Honor `prefers-color-scheme`, which carries the viewer's
OS preference. If the page ships its own theme switch, stamp
`data-theme="dark"` / `data-theme="light"` on the root element and let that
override the media query in **both** directions. The robust pattern is
token-level: define the palette as custom properties on `:root`, redefine only
the tokens under `@media (prefers-color-scheme: dark)` — style components through
the tokens, never directly inside the media query — then redefine them again
under `:root[data-theme="dark"]` and `:root[data-theme="light"]`. Give the second
theme the same care as the first: don't naively invert; keep contrast legible and
the accent working on both grounds. A design that deliberately commits to one
visual world (a neon arcade screen, a letterpress invitation) may stay
single-theme — make it a choice, not an omission.

**Let layout do the spacing.** Lay out sibling groups with flex or grid and
`gap`, not per-element margins that silently collapse or double. Wide content —
tables, code, diagrams — gets `overflow-x: auto` on its own container so the page
body never scrolls sideways. Reach for `font-variant-numeric: tabular-nums`
wherever digits line up in columns.

**Avoid the AI-generated look.** AI-generated design currently clusters around a
few looks: warm cream (#F4F1EA) with a serif display and terracotta accent;
near-black with a lone acid-green or vermilion pop; broadsheet hairline rules
with dense columns; a purple-to-blue gradient hero on white; Inter or Space
Grotesk as the "safe" face; emoji as section markers; everything centered;
`rounded-lg` everywhere; an accent bar or rail on rounded cards. Where the user
pins down a visual direction, follow it exactly — their words always win,
including when they ask for one of these looks. Where nothing is specified, don't
spend that freedom on one of these defaults.

**Build cleanly.** Be cognizant of overlapping elements, cascade collisions, and
silent font fallbacks; visual bugs hide in the gap between source and output.
Close every non-void element, double-quote attributes, give keyboard focus a
visible state, respect `prefers-reduced-motion`. For generative or decorative
graphics, reach for Canvas or WebGL rather than hand-authoring long SVG path
data.

**Watch CSS specificity.** It is easy to generate classes that cancel each other
out — a type-based selector like `.section` fighting an element-based one like
`.cta` over padding and margins between sections. Structure the cascade so it
doesn't silently undo your spacing.

**Write the copy as design material.** Words are not decoration. Write from the
user's side of the screen — name things by what people recognize, not how the
system is built (a person manages *notifications*, not *webhook config*). Active
voice; a control says exactly what happens ("Publish", then a toast that says
"Published"). Errors explain what went wrong and how to fix it — no apologies, no
vagueness. Specific beats clever.

**Structure is information.** Structural devices — numbering, eyebrows, dividers,
labels — should encode something true about the content, not decorate it. Many
generic designs use numbered markers (01 / 02 / 03), but that is only appropriate
if the content actually is a sequence, like a real process or a typed timeline
where order carries information the reader needs. Question whether such choices
make sense before incorporating them.

**When it's a UI, not a document.** A dashboard or tool is scanned and operated,
not read top-to-bottom, so the craft shifts from typography to information
design. Surface the summary before the detail; encode state in form as well as
number — a pill, a chip, a severity stripe — so what needs attention reads at a
glance. Semantic color (good / warning / critical) is separate from the accent
hue and doesn't count as your accent. Give sparklines and charts the same care as
type. What's interactive should look interactive.

## Process

Before writing code, sketch a short design plan — a compact token system with
color, type, and layout:

- **Color**: describe the palette as 4–6 named hex values.
- **Type**: typefaces for 2+ roles — a characterful display face used with
  restraint, a complementary body face, and a utility face for captions or data
  if needed.
- **Layout**: a layout concept in one or two sentences.

Then build, following the plan and deriving every color and type decision from
it.

## When the request is editorial

The stance shifts: the client has already rejected proposals that felt templated,
and is paying for a distinctive point of view. Make opinionated calls, and take
one real aesthetic risk where it serves the work.

Review the design plan against the subject before building: if any part of it
reads like the generic default you would produce for any similar page, revise
that part, and note what you changed and why. Only after you've confirmed the
plan's uniqueness do you write the code, following the revised plan exactly.

- The hero is a thesis: open with the most characteristic thing in the subject's
  world — headline, image, live demo, interactive moment.
- Typography carries the personality of the page. Pair the display and body faces
  deliberately, not the same families you would reach for on any other project,
  and set a clear type scale with intentional weights, widths, and spacing. Make
  the type treatment itself a memorable part of the design, not a neutral
  delivery vehicle for the content.
- Leverage motion deliberately. Think about where and if animation can serve the
  subject: a page-load sequence, a scroll-triggered reveal, hover
  micro-interactions, ambient atmosphere. An orchestrated moment usually lands
  harder than scattered effects. Sometimes less is more — extra animation
  contributes to the feeling that the design is AI-generated.
- Match complexity to the vision. Maximalist directions need elaborate execution;
  minimal directions need precision in spacing, type, and detail. Elegance is
  executing the chosen vision well.
- Spend your boldness in one place; keep everything around it quiet. If the
  accent fights the ground, shift it toward analogous or drop saturation rather
  than replacing it.

## Check your work

The page is served over the local network, so you can look at it. Open or
screenshot the result and eyeball it for label collisions, overflow, contrast,
and the mobile width before calling it done — a picture is worth a thousand
tokens. `ava.mcps.chrome` can drive a browser against the URL `ava.ui.serve` returned.

Provenance and local adaptations: [`../../VENDORED.md`](../../VENDORED.md).
