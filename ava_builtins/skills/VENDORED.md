# Vendored skills

Skills under `ava_builtins/skills/` that were **copied from an upstream source
and adapted**, rather than authored here. One row per vendored skill.

Vendoring is the settled posture for external capability packs
([`default-skills.md`](../default-skills.md)): an unadapted install carries
harness-specific machinery that does not hold in Ava, so we copy, strip, rewire
to `ava.*` idioms, and own the sync cost.

Anything landed here is stamped `trust="builtin"` by converge
(`cli/commands/_converge_skills.py`) and bypasses the install-time supply-chain
scanner — so a vendored pack must be **read in full before it is committed**,
not merely copied.

## The license bar (2026-08-14)

A vendored pack ships from this repo under Apache-2.0, which means the row below
has to name a **license grant that actually covers the copied files** — a
published `LICENSE` in the upstream repo the files came from, or an explicit
grant in the files themselves.

"A similarly named skill elsewhere is Apache-2.0" is not that. It was the basis
recorded for `ava-ui/design` + `ava-ui/dataviz`, vendored 2026-07-30 out of the
**closed-source** Claude Code CLI binary (`@anthropic-ai/claude-code` 2.1.220):
the row pointed at `frontend-design` in
[`anthropics/skills`](https://github.com/anthropics/skills), which does ship an
Apache-2.0 `LICENSE.txt` — but that repo carries no `dataviz` and no
`artifact-design`, so the license of a neighbouring skill was never evidence
about the copied files. Both skills were removed from this repo on 2026-08-14.

Content extracted from a closed-source distribution does not become
redistributable by resembling something that is. Vendor from a source whose
license you can point at.

## Vendored frontend libraries (`ava-ui/widgets/markdown/vendor/`)

`ava-ui/widgets/markdown` ships a zero-build HTML renderer whose dependencies
are vendored under `vendor/` (no CDN pulling): KaTeX **0.16.21** (`katex.min.js`
+ `katex.min.css` + fonts, MIT), marked **14.1.3** (`marked.min.js`, MIT),
highlight.js **11.10.0** (`highlight.min.js`, BSD-3-Clause), DOMPurify **3.2.3**
(`purify.min.js`, Apache-2.0/MPL-2.0), and the GitHub-markdown CSS pair
(`github.min.css` / `github-dark.min.css`). These are single-file minified
releases copied unmodified; refresh by re-downloading the listed versions (no
build step), and record any version bump here.
