# Vendored skills

Skills under `ava_builtins/skills/` that were **copied from an upstream source
and adapted**, rather than authored here. One row per vendored skill; the
`track-vendored-skills` skill ([`.agents/skills/track-vendored-skills/SKILL.md`](../../.agents/skills/track-vendored-skills/SKILL.md))
diffs upstream against the recorded version and reports drift.

Vendoring is the settled posture for external capability packs
([`future/coding/default-skills.md`](../default-skills.md)):
an unadapted install carries harness-specific machinery that does not hold in
Ava, so we copy, strip, rewire to `ava.*` idioms, and own the sync cost.

Anything landed here is stamped `trust="builtin"` by converge
(`cli/commands/_converge_skills.py`) and bypasses the install-time supply-chain
scanner — so a vendored pack must be **read in full before it is committed**,
not merely copied.

| Skill | Upstream source | Upstream version | Vendored | License |
|---|---|---|---|---|
| `ava-ui/design` | Claude Code bundled skill `artifact-design` | CLI 2.1.220 | 2026-07-30 | Apache-2.0 |
| `ava-ui/dataviz` | Claude Code bundled skill `dataviz` | CLI 2.1.220 | 2026-07-30 | Apache-2.0 |

Both are Anthropic-authored skills distributed inside the Claude Code CLI
binary (`@anthropic-ai/claude-code`), pinned by its build markers:

    VERSION     2.1.220
    BUILD_TIME  2026-07-24T22:17:45Z
    GIT_SHA     4073f59596e272f39393db4f96abc5f4b10eff21

**Re-fetching these is not a plain `git pull`.** Neither skill ships as a loose
file. `artifact-design` is a single SKILL.md embedded in the binary with no
supporting files at all; `dataviz`'s `references/` + `scripts/` are lazily
unpacked at runtime to `$TMPDIR/bundled-skills/<version>/<random-hex>/dataviz/`
(the hex is regenerated per extraction, so the path is never stable), while its
SKILL.md body is likewise injected straight into the prompt and never written to
disk. To refresh: invoke the skills in a Claude Code session of the target
version, then read the extracted tree for the file-backed parts.

License: the closely-related public sibling `frontend-design` in
[`anthropics/skills`](https://github.com/anthropics/skills) (commit `b29e7cf6`,
2026-07-24) ships an explicit `LICENSE.txt` = Apache-2.0, the same license as
this repo — so this text is redistributable in-tree. That public skill is a
plausible future upstream to track instead, since it *is* a plain file.

## Vendored frontend libraries (`ava-ui/widgets/markdown/vendor/`)

`ava-ui/widgets/markdown` ships a zero-build HTML renderer whose dependencies
are vendored under `vendor/` (no CDN pulling): KaTeX **0.16.21** (`katex.min.js`
+ `katex.min.css` + fonts, MIT), marked **14.1.3** (`marked.min.js`, MIT),
highlight.js **11.10.0** (`highlight.min.js`, BSD-3-Clause), DOMPurify **3.2.3**
(`purify.min.js`, Apache-2.0/MPL-2.0), and the GitHub-markdown CSS pair
(`github.min.css` / `github-dark.min.css`). These are single-file minified
releases copied unmodified; refresh by re-downloading the listed versions (no
build step), and record any version bump here.

## Local adaptations

### `ava-ui/design` (from `artifact-design`)

Design content kept in full — the calibration ("how much design this request
warrants"), typography, neutrals, both-themes discipline, layout/spacing, the
AI-look catalog, copywriting, structure-is-information, and the dashboard
information-design section are all load-bearing and unchanged in substance.

Stripped / rewired:

- **Artifact publishing harness** — every "artifact" is a **page**; no `Artifact`
  tool, no claude.ai publish/redeploy/gallery flow, no favicon or `<title>`
  contract, no runtime-capabilities cross-reference.
- **CSP claim → real Ava constraint.** Upstream justifies inlining fonts with
  "the Artifact CSP blocks font CDNs." Ava pages are plain static files served
  off the agent runner, so the honest reason is stated instead: no build step, no
  bundler, and a webfont CDN fails silently into a fallback face.
- **Theme toggle.** Upstream describes a platform toggle that stamps
  `data-theme` on the root. Ava pages have no platform chrome, so this reads as:
  honor `prefers-color-scheme`, and *if the page ships its own switch*, stamp
  `data-theme` and let it win both directions. The token-level CSS pattern —
  the actual value — is unchanged.
- **Ava wiring added** — scope note pointing at `ava-ui` for serving
  (`ava.ui.serve`) and `ava-ui.dataviz` for charts; a closing "check your work"
  step naming `ava.web` for driving a browser at the served URL.

### `ava-ui/dataviz`

`scripts/validate_palette.{py,js}` are **byte-identical to upstream** and are
excluded from ruff + pyright in `pyproject.toml` to keep them that way.

`references/*.md` (7 files) carry the design content unchanged; the only edits
are five one-line path/phrasing fixes, so a diff against upstream stays readable:

- `color-formula.md`, `anti-patterns.md`, `components.md` — lead with
  `validate_palette.py` instead of `node …validate_palette.js` (the JS build
  stays documented for in-page use).
- `color-formula.md` — "this skill's base directory, shown at the top of the
  prompt" is a Claude Code harness affordance; replaced with
  `ava.help(ava.skills.ava_ui.dataviz)`, which prints the path in Ava.
- `palette.md` — "the viewer's theme toggle" (a claude.ai platform control)
  becomes "the page's own theme toggle (when it ships one)".

`SKILL.md` adaptations:

- **Medium list rewritten** — "an HTML or React artifact … or a chart shared into
  Slack" becomes the media Ava actually produces: a served HTML/SVG page, a
  plotting library, or a rendered image.
- **Validator path leads with Python.** Upstream leads with
  `node scripts/validate_palette.js`. `validate_palette.py` is stdlib-only and
  Ava agents execute Python natively (node is not guaranteed on a runner), so the
  Python invocation is the documented default with a worked `ava.shell.run`
  call; the JS build stays documented for running live inside the page.
- **Ava wiring added** — scope note pointing at `ava-ui.design` for page-level
  look and `ava-ui` for serving; `ava.help(...)` named as the way to resolve the
  skill's own directory path for the script call.

## Placement note

Both live **nested under `ava-ui`** rather than as top-level siblings. The
system-prompt capability index is a whitelist
(`AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT`) that already contains `ava-ui`; a new
top-level skill would be absent from it until that config default changed on every
cluster. Nesting makes the pack ride the entry skill that is already recalled, and
`ava-ui`'s own index line is unaffected — `_flatten()` emits a root skill
alongside its children, so both parent and children keep their descriptions.
