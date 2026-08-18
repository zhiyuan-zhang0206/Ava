# OKF node ceiling: raise the character cap to 8000

**Decision (2026-07-29):** `MAX_CHARS` goes from 6000 to 8000, and the linter
gains a non-blocking headroom warning. The cap had stopped forcing hierarchy and
started forcing fact deletion; raising it restores the forcing function at a size
where complying means splitting a topic rather than cutting a sentence.

## Context

The OKF per-node ceiling was 200 lines / 6000 characters of decoded UTF-8. The
character cap is the one that binds: across 160 nodes the largest is 129 lines,
so `MAX_LINES` has never fired in the graph's history. Every author who has been
stopped by a node ceiling was stopped by the character cap.

Measured on `main` at fa544ef2, the top of the size distribution was:

```
6000  room    0  shared/agents-contract.ava.okf.md
5999  room    1  okf/skills.ava.okf.md
5998  room    2  shared.ava.okf.md
5997  room    3  okf/plugins.ava.okf.md
5995  room    5  gateway/routers.ava.okf.md
5995  room    5  services.ava.okf.md
5991  room    9  shared/log.ava.okf.md
```

Fifteen nodes sat within 100 characters of the cap and 28 within 500 — then the
distribution falls off a cliff, with only 33 nodes above 5000 and 50 above 4500.
Content does not naturally stack in the last nine characters below a hard
boundary. That shape is the fingerprint of repeated trim-to-fit, and the history
names the mechanism: `af111684` records "14 处 E007 裁剪：超 6000 字符文件压缩至
阈值内" — fourteen files compressed to fit — and PR #932's description says of
`shared.ava.okf.md` that a fact was "trimmed elsewhere to stay under the
6000-char node ceiling".

So the cap had inverted its own purpose. It exists to force hierarchy; it was
instead forcing documented facts to be deleted to make room for new ones.

## Decision

Raise `MAX_CHARS` to 8000 and add a non-blocking headroom warning (`W010`) that
reports before the wall rather than at it.

Why 8000 and not the alternatives:

- It gives each previously-saturated node roughly 2000 characters — 20-25 lines
  of prose, three or four paragraphs — so the next author is not immediately back
  against the wall. A stingier bump would just relocate the problem.
- No node exceeds it today, so the raise is not a retroactive blessing of
  existing bloat: it changes what is *allowed next*, and nothing about what has
  already been written.
- It keeps the character cap binding well before the line cap at every real
  density (8000 characters is 80 lines at the corpus median of 70 chars/line, and
  about 60 at the density of the densest nodes). The cap therefore still forces
  hierarchy — it just does so at a size where the decision is a judgement about
  topics rather than a fight over sentences.
- It is not the line-cap equivalent. At the corpus median density, 200 lines
  implies about 14000 characters; adopting that would make the character cap dead
  code in the same way `MAX_LINES` already is, and remove the forcing function
  entirely.

## Alternatives rejected

### The ceiling is correct, and 28 nodes genuinely need splitting

This is refuted by the shape of the distribution and by the history above. A cap
that were correctly reporting "these nodes have outgrown one topic" would show
nodes *over* it, spread out. Fifteen nodes stacked in a 100-character band below
it is a record of authors trimming, not of content sizes.

It is also refuted by *which* nodes saturated. Five of the ten most-saturated
nodes are index-layer domain overviews (`skills`, `shared`, `plugins`,
`services`, `mcps`). An index node's size is a function of how many children its
domain has, not of its author's verbosity, and it grows monotonically as the repo
grows. It is also the one kind of node that cannot comply: its single job is to be
the complete list of a domain, so splitting it yields half an index. A ceiling
whose stated purpose is "force hierarchy" is incoherent when applied to the node
whose purpose is to *be* the hierarchy's index. The cap was biting hardest
exactly where compliance was impossible.

### The ceiling is right but the unit is wrong — exempt code blocks and tables

Plausible in the abstract — fenced code and table pipes do inflate a character
count without a matching increase in reading burden — but measurably the wrong
lever here. Repo-wide, fenced code is 4.0% of all OKF characters and table rows
5.9%. Of the sixteen nodes nearest the cap, eight contain **zero** code blocks and
zero table rows, including all four that had no room at all
(`shared/agents-contract`, `okf/skills`, `okf/shared`,
`gateway/routers`). Exempting code and tables would have delivered exactly zero
headroom to the nodes that needed it. The one node it would substantially help,
`frontend/src/frontend.ava.okf.md` at 48% code, was not in difficulty.

The observation is true and the remedy is unrelated to the problem, so adopting
it would have added an exemption rule to the linter and left the saturation in
place.

## Consequences

- `MAX_CHARS` = 8000, `WARN_MARGIN` = 800 in `scripts/lint_ava_okf.py`. The
  warning names the node, its size and its remaining room, and does not change
  the exit code.
- `MAX_LINES` is left at 200 and remains inert — no node is near it. It is kept as
  a backstop against a pathological many-short-lines document rather than as a
  live constraint. If it is ever to become meaningful it needs its own decision;
  lowering it here would have put `ava_builtins/plugins/ava_fleet/spawn.ava.okf.md`
  (129 lines) over a cap it has never been asked to meet.
- The saturated nodes are *not* retro-expanded. They keep whatever shape trimming
  left them in; what changes is that the next author has somewhere to write.
  Several of them (`shared/log`, `gateway/routers`, `services/watchdog`) are worth
  re-reading for facts that were compressed to the point of obscurity, but
  restoring compressed prose is editorial work, separate from moving the cap.
