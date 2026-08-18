"""Skill / plugin name normalization — the one dash-underscore projection.

**Dash is canonical** at every human-facing surface: on-disk skill directory
names, SKILL.md frontmatter `name:`, CLI arguments, gateway API fields, the
agent-config skill lists, the frontend. That is the Agent Skills / Claude Code
ecosystem spelling, so a skill directory authored for Ava is a skill directory
anywhere else and vice versa.

**Underscore is the Python projection and nothing else.** `-` is not an
identifier character, so the CodeAct namespace reaches the
`write-a-pr-description/` directory as `ava.skills.write_a_pr_description`.
`ava.skills.target()` renders that projection outward; this module is the
inbound half — the single place a name arriving from a human, a preset row, an
API body, or a legacy underscore directory is folded to a comparison key.

`.` separates namespace segments in the loadable Python projection (the skill
tree's shape *is* the folder shape, so a plugin's contribution is a folder like
any other). The display identifier renders the same tree with `:` between
segments (`ava.skills.web-ai:deep-research`); `match_key` folds an inbound
ecosystem-style `plugin:skill` reference (or dotted input) onto that same key,
so every spelling resolves to one skill.

**Never build a filesystem path out of `match_key` output.** A few surfaces use
a name as a path segment (`ava skill register <name>`, `PUT /api/skills`), and
the directory on disk may legitimately be spelled either way. Use `find` to map
the inbound spelling onto the real on-disk / registry spelling, then use that.
"""

from __future__ import annotations

from collections.abc import Iterable


def match_key(name: str) -> str:
    """Fold an inbound name to its comparison key: `-` -> `_`, `:` -> `.`.

    Two names denote the same skill iff their keys are equal, so call this on
    BOTH sides of every comparison. It doubles as the attribute-form renderer
    for a single segment (`ava.skills.<seg>`), which is the same fold.
    """
    return name.replace("-", "_").replace(":", ".")


def display_name(name: str) -> str:
    """Render a name in canonical display form: `_` -> `-`.

    Applied on the way out (`ava.skills.identifier`, CLI listings, API
    responses) so even a legacy underscore directory presents the canonical
    spelling. Namespace `.` separators are left alone.
    """
    return name.replace("_", "-")


def find(name: str, candidates: Iterable[str]) -> str | None:
    """Return the candidate `name` refers to, or None when nothing matches.

    An exact hit wins; otherwise the *unique* candidate sharing its
    `match_key`. Ambiguity reads as "not found" — the loader refuses a
    post-normalization duplicate outright (`ava.skills.SkillNameCollision`), so
    there is no correct pick to make here.

    This is the bridge for every surface where an inbound name has to become a
    real registry key or directory name.
    """
    pool = list(candidates)
    if name in pool:
        return name
    key = match_key(name)
    hits = [c for c in pool if match_key(c) == key]
    return hits[0] if len(hits) == 1 else None


class SkillIdentityMismatch(ValueError):  # noqa: N818
    """A skill's install-point directory and its SKILL.md frontmatter fold to
    different keys.

    The directory is the identity source; the frontmatter `name:` is the
    display declaration. The two must denote the same skill (equal
    `match_key`), and a constructor that sees them disagree fails fast —
    the alternative is one skill silently presenting under two names
    (#980, #1702, and the dual-row registry crash all grew from that hole).
    """


class SkillIdentity:
    """One skill's name as a single typed object across every surface.

    A skill's name is spelled differently on seven surfaces — disk directory
    name, SKILL.md frontmatter `name:`, install-registry row, CLI argument,
    API field, frontend display, Python attribute. This object is the fold:
    constructed at a boundary from whatever spelling arrived there, it carries
    the raw spelling (`name`), the comparison key (`key`, via `match_key`),
    and the canonical display form (`display`, via `display_name`) — so a
    comparison can never forget to fold, and a render can never forget to
    canonicalize.

    Construction semantics follow the design invariant "one skill = one
    `match_key` = one directory = one registry row":

    - `from_dir(name)` — the install point (disk directory) is the *source of
      identity*: what the skill IS.
    - `from_frontmatter(name)` — the `name:` declaration is the *display
      claim*: what the skill is CALLED. It must fold to the same key as the
      directory; `verify()` enforces that at the scanner / installer.
    - `from_cli(name)` — an inbound CLI / API / config reference: folded onto
      the real identity by key, never compared raw.

    Instances are immutable and cheap; equality and hashing are by key, so
    they work directly as dict keys / set members where the fold matters.
    """

    __slots__ = ("display", "key", "name")

    def __init__(self, name: str) -> None:
        self.name = name
        self.key = match_key(name)
        self.display = display_name(name)

    @classmethod
    def from_dir(cls, name: str) -> SkillIdentity:
        """Identity from the install-point directory name (the source of
        identity — see the class docstring)."""
        return cls(name)

    @classmethod
    def from_frontmatter(cls, name: str) -> SkillIdentity:
        """Identity from the SKILL.md frontmatter `name:` (the display
        declaration)."""
        return cls(name)

    @classmethod
    def from_cli(cls, name: str) -> SkillIdentity:
        """Identity from an inbound CLI / API / config reference — folded to
        the comparison key, to be matched against registry rows / directories
        through `key`, never compared raw."""
        return cls(name)

    def verify(self, other: SkillIdentity) -> None:
        """Fail fast when `self` and `other` denote different skills.

        `from_dir` vs `from_frontmatter` is the designed pair: the directory
        is the identity source, the frontmatter name is the display claim,
        and a fold mismatch means the skill would present under a name that
        is not its own."""
        if self.key != other.key:
            raise SkillIdentityMismatch(
                f"skill identity mismatch: directory '{self.name}' (key "
                f"{self.key!r}) vs frontmatter name '{other.name}' (key "
                f"{other.key!r}) — dash and underscore are the same name "
                "(shared/skill_names.py); the frontmatter name must fold to "
                "the directory name. Rename one; the directory is canonical."
            )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SkillIdentity) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def __repr__(self) -> str:
        return f"SkillIdentity({self.name!r})"
