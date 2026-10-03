"""The seed OKF validator of the AvaMemory template reports every rule violation it was written for.

One crafted bundle trips each check; the expected messages pin the validator's behavior so its
functions can be restructured without changing what it reports.
"""

import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

_VALIDATE = Path(__file__).resolve().parents[1] / "template" / "validate.py"


def _validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_template_validate", _VALIDATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def fm(**kw: str) -> str:
    lines = ["---"]
    for k, v in kw.items():
        lines.append(f"{k}: {v}")
    lines.append("---\nbody\n")
    return "\n".join(lines)


def build(root: Path) -> None:
    w(
        root,
        "MEMORY.md",
        "- [Good](good.md) — x\n- [Wrong title](titled.md)\n- [dup](dup.md)\n- [dup2](dup.md)\n- [gone](gone.md)\n- [http](https://x.md)\n- [Dirpointed](pointed/index.md)\n",
    )
    w(root, "good.md", fm(type="note", ava_agent="1", description="d", tags="[type/user]"))
    w(
        root,
        "titled.md",
        fm(type="note", ava_agent="1", title="Real Title", description="d", tags="[type/feedback]"),
    )
    w(root, "dup.md", fm(type="note", ava_agent="1", description="d", tags="[type/env, project]"))
    w(
        root,
        "orphan.md",
        fm(type="note", ava_agent="1", description="d", tags="[type/role, type/user]"),
    )
    w(root, "nofm.md", "no frontmatter\n")
    w(root, "unclosed.md", "---\ntype: a\n")
    w(root, "badyaml.md", "---\n: : :\n  - [\n---\nx\n")
    w(root, "notmap.md", "---\n- a\n- b\n---\nx\n")
    w(root, "notype.md", "---\nava_agent: 1\ndescription: d\ntags: [a, 1]\n---\nx\n")
    w(root, "nulls.md", '---\ntype: ""\nava_agent: null\ndescription: ""\ntags: notalist\n---\nx\n')
    w(
        root,
        "ts.md",
        fm(
            type="n",
            ava_agent="1",
            description="d",
            tags="[type/user]",
            timestamp="2026-01-01T00:00:00",
        ),
    )
    w(
        root,
        "ts2.md",
        fm(type="n", ava_agent="1", description="d", tags="[type/user]", timestamp="bogus"),
    )
    w(
        root,
        "gen.md",
        fm(type="n", ava_agent="1", description="d", tags="[type/user]", generated="x"),
    )
    w(
        root,
        "gen2.md",
        '---\ntype: n\nava_agent: 1\ndescription: d\ntags: [type/user]\ngenerated:\n  by: ""\n  at: nope\n---\n',
    )
    w(
        root,
        "misread.md",
        "---\ntype: n\nava_agent: 1\ndescription: has # hash\ntags: [type/user]\n---\n",
    )
    w(root, "badtype.md", fm(type="n", ava_agent="1", description="d", tags="[type/bogus, repo]"))
    w(
        root,
        "projects/alpha/a.md",
        fm(type="n", ava_agent="1", description="d", tags="[type/project, alpha]"),
    )
    w(
        root,
        "misplaced.md",
        fm(type="n", ava_agent="1", description="d", tags="[type/project, alpha]"),
    )
    w(
        root,
        "both.md",
        fm(type="n", ava_agent="1", description="d", tags="[type/project, alpha, beta, ava]"),
    )
    w(
        root,
        "projects/beta/b.md",
        fm(type="n", ava_agent="1", description="d", tags="[type/project]"),
    )
    w(
        root,
        "pointed/index.md",
        "- [Real Title](a.md)\n- [Wrong](b.md)\n- [dup](a.md)\n- [missing](zzz.md)\n- [d](sub/index.md)\n",
    )
    w(
        root,
        "pointed/a.md",
        fm(type="n", ava_agent="1", title="Real Title", description="d", tags="[type/user]"),
    )
    w(root, "pointed/b.md", fm(type="n", ava_agent="1", description="d", tags="[type/user]"))
    w(root, "pointed/c.md", fm(type="n", ava_agent="1", description="d", tags="[type/user]"))
    w(root, "noindex/n.md", fm(type="n", ava_agent="1", description="d", tags="[type/user]"))
    w(root, "fmindex/index.md", "---\nx: 1\n---\n- [n](n.md)\n")
    w(root, "fmindex/n.md", fm(type="n", ava_agent="1", description="d", tags="[type/user]"))
    w(root, ".hidden/h.md", fm(type="n"))
    w(
        root,
        "fc.md",
        "<!-- fact-check: test -d /nonexistent_x -->\n<!-- fact-check: ! test -d / -->\n<!-- fact-check: rm -rf x -->\n<!-- fact-check: test -d / -->\n",
    )
    for i in range(22):
        w(root, f"many/n{i}.md", fm(type="n", ava_agent="1", description="d", tags="[type/user]"))
    for i in range(21):
        (root / f"dirs/d{i}").mkdir(parents=True)
    w(root, "unicode.md", "x")
    (root / "bad_utf.md").write_bytes(b"\xff\xfe")


_EXPECTED = {
    "validate_factchecks": [
        "fc.md: fact-check FAILED: !test -d /",
        "fc.md: fact-check FAILED: test -d /nonexistent_x",
        "fc.md: fact-check command not whitelisted: 'rm -rf x' (allowed: ['git', 'ls', 'test'])",
    ],
    "validate_file": {
        ".hidden/h.md": [
            "Missing or empty 'description' — it is the only part of a "
            "note a pointer line and a search result show",
            "Missing required field 'ava_agent'",
        ],
        "bad_utf.md": ["File is not valid UTF-8"],
        "badyaml.md": [
            "Invalid YAML: while parsing a block mapping\n"
            "expected <block end>, but found ':'\n"
            '  in "<unicode string>", line 1, column 1:\n'
            "    : : :\n"
            "    ^"
        ],
        "fc.md": ["Missing YAML frontmatter (must start with ---)"],
        "gen.md": ["Field 'generated' must be a mapping"],
        "gen2.md": [
            "Field 'generated.at' does not look like ISO 8601: 'nope'",
            "Field 'generated.by' must be a non-empty string",
        ],
        "misread.md": [
            "Field 'description' is YAML-misread: raw text differs from the "
            "parsed value (an unquoted ' #' or ': ' truncates it) — wrap the "
            "value in double quotes"
        ],
        "nofm.md": ["Missing YAML frontmatter (must start with ---)"],
        "notmap.md": ["Frontmatter must be a YAML mapping"],
        "notype.md": ["EXC AttributeError"],
        "nulls.md": [
            "Field 'ava_agent' must not be null",
            "Field 'tags' must be a list",
            "Field 'type' must be a non-empty string",
            "Missing or empty 'description' — it is the only part of a note a "
            "pointer line and a search result show",
        ],
        "orphan.md": ["2 type tags (type/role, type/user) — exactly one"],
        "ts2.md": ["Field 'timestamp' does not look like ISO 8601: 'bogus'"],
        "unclosed.md": ["Unclosed YAML frontmatter"],
        "unicode.md": ["Missing YAML frontmatter (must start with ---)"],
    },
    "validate_indexes": [
        ".: has 21 notes but no index.md (OKF §8)",
        "fmindex/index.md: index files must have no frontmatter (OKF §8)",
        "many: has 22 notes but no index.md (OKF §8)",
        "noindex: has 1 notes but no index.md (OKF §8)",
        "pointed/index.md: duplicate pointer a.md (2x)",
        "pointed/index.md: orphan entry (no pointer): c.md",
        "pointed/index.md: pointer target missing: sub/index.md",
        "pointed/index.md: pointer target missing: zzz.md",
        "pointed/index.md: pointer title mismatch for a.md: 'dup' != 'Real Title'",
        "pointed/index.md: pointer title mismatch for b.md: 'Wrong' != 'b'",
        "projects/alpha: has 1 notes but no index.md (OKF §8)",
        "projects/beta: has 1 notes but no index.md (OKF §8)",
    ],
    "validate_pointers": [
        "MEMORY.md: duplicate pointer dup.md (2x)",
        "MEMORY.md: pointer target missing: gone.md",
        "MEMORY.md: pointer title mismatch for titled.md: 'Wrong title' != 'Real Title'",
        "directory without pointer: .hidden/",
        "directory without pointer: fmindex/",
        "directory without pointer: many/",
        "directory without pointer: noindex/",
        "directory without pointer: projects/",
        "orphan note (no pointer in MEMORY.md): bad_utf.md",
        "orphan note (no pointer in MEMORY.md): badtype.md",
        "orphan note (no pointer in MEMORY.md): badyaml.md",
        "orphan note (no pointer in MEMORY.md): both.md",
        "orphan note (no pointer in MEMORY.md): fc.md",
        "orphan note (no pointer in MEMORY.md): gen.md",
        "orphan note (no pointer in MEMORY.md): gen2.md",
        "orphan note (no pointer in MEMORY.md): misplaced.md",
        "orphan note (no pointer in MEMORY.md): misread.md",
        "orphan note (no pointer in MEMORY.md): nofm.md",
        "orphan note (no pointer in MEMORY.md): notmap.md",
        "orphan note (no pointer in MEMORY.md): notype.md",
        "orphan note (no pointer in MEMORY.md): nulls.md",
        "orphan note (no pointer in MEMORY.md): orphan.md",
        "orphan note (no pointer in MEMORY.md): ts.md",
        "orphan note (no pointer in MEMORY.md): ts2.md",
        "orphan note (no pointer in MEMORY.md): unclosed.md",
        "orphan note (no pointer in MEMORY.md): unicode.md",
    ],
    "validate_project_home": [
        "misplaced.md: tag contains project 'alpha' but the note is not under "
        "projects/alpha/ — project-specific notes belong in the project tree "
        "(user ruling 2026-08-30); drop the project tag for cross-project "
        "knowledge"
    ],
    "validate_structure": [
        "(root): 22 md files, over the 20 cap — split into topical subdirectories",
        "dirs: 21 subdirectories, over the 20 cap — merge or re-group",
        "many: 22 md files, over the 20 cap — split into topical subdirectories",
    ],
    "validate_type_tag": [
        "badtype.md: invalid type tag 'type/bogus' — use one of ['type/env', "
        "'type/feedback', 'type/project', 'type/reference', 'type/role', "
        "'type/user']",
        "badtype.md: junk tag 'repo' — drop it",
        "dup.md: junk tag 'project' — drop it",
        "notype.md: missing type/<x> tag (user ruling 2026-08-30 tag discipline)",
        "nulls.md: missing type/<x> tag (user ruling 2026-08-30 tag discipline)",
        "orphan.md: multiple type tags ['type/role', 'type/user'] — keep exactly one",
    ],
}


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("pool")
    build(root)
    return root


@pytest.mark.parametrize(
    "name",
    [
        "validate_type_tag",
        "validate_project_home",
        "validate_structure",
        "validate_factchecks",
        "validate_pointers",
        "validate_indexes",
    ],
)
def test_bundle_validator_reports_its_violations(bundle: Path, name: str) -> None:
    check: Callable[[Path], list[str]] = getattr(_validator(), name)
    assert sorted(check(bundle)) == _EXPECTED[name]


def test_file_validator_reports_each_frontmatter_violation(bundle: Path) -> None:
    validate_file = _validator().validate_file
    found: dict[str, list[str]] = {}
    for note in sorted(bundle.rglob("*.md")):
        try:
            errors = validate_file(note)
        except AttributeError as exc:  # a non-string tag crashes the tag scan (pinned as-is)
            errors = ["EXC " + type(exc).__name__]
        if errors:
            found[str(note.relative_to(bundle))] = sorted(errors)
    assert found == _EXPECTED["validate_file"]
