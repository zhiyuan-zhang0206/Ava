import pytest

from base.deploy.git.repo_change import classify_change, is_doc_path


@pytest.mark.parametrize(
    "path",
    [
        "docs/contributing.md",
        "docs/decisions/foo.md",
        "docs/postmortems/x.md",
        "future/y.md",
        "docs/conventions/z.md",
        "okf/index.ava.okf.md",
        "assets/img.png",
        "schedules/x.json",
        "README.md",
        "AGENTS.md",
    ],
)
def test_is_doc_path(path: str) -> None:
    assert is_doc_path(path)


@pytest.mark.parametrize("path", ["ui/web/AGENTS.md", "gateway/README.md"])
def test_is_doc_path_leaves_nested_code_docs_to_their_directory(path: str) -> None:
    assert not is_doc_path(path)


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["ui/web/app/page.tsx", "ui/web/src/lib/transport/api.ts"], (True, False)),
        (["agent/graph/exec/node.py"], (False, True)),
        (
            [
                "docs/decisions/foo.md",
                "docs/postmortems/x.md",
                "future/y.md",
                "docs/conventions/z.md",
                "okf/index.ava.okf.md",
                "assets/img.png",
                "schedules/x.json",
            ],
            (False, False),
        ),
        (["README.md", "AGENTS.md"], (False, False)),
        (["ui/web/AGENTS.md"], (True, False)),
        (["gateway/README.md"], (False, True)),
        ([], (False, False)),
        (["ui/web/app/page.tsx", "agent/graph/exec/node.py"], (True, True)),
    ],
)
def test_classify_change(paths: list[str], expected: tuple[bool, bool]) -> None:
    assert classify_change(paths) == expected


def test_component_okf_docs_do_not_select_a_code_suite() -> None:
    paths = [
        "scripts/lint/docs/changed-files-mode.ava.okf.md",
        "base/deploy/docs/deploy.ava.okf.md",
        "gateway/docs/gateway.ava.okf.md",
        "ui/web/src/docs/frontend-components/page.ava.okf.md",
        "ava_builtins/skills/ava-guide/docs/agents.ava.okf.md",
    ]
    for path in paths:
        assert is_doc_path(path), path
        assert classify_change([path]) == (False, False), path


def test_component_doc_rules_preserve_code_and_test_inputs() -> None:
    cases = {
        "scripts/lint/tests/docs/expected.ava.okf.md": (False, True),
        "base/lm/tests/docs/expected.ava.okf.md": (False, True),
        "gateway/docs/tests/expected.ava.okf.md": (False, True),
        "tests/docs/expected.ava.okf.md": (False, True),
        "ui/web/tests/docs/expected.ava.okf.md": (True, False),
        "gateway/gateway.ava.okf.md": (False, True),
        "gateway/docs/config.json": (False, True),
        "gateway/docs/AGENTS.md": (False, True),
        "ava_builtins/skills/ava-guide/docs/SKILL.md": (False, True),
    }
    for path, expected in cases.items():
        assert not is_doc_path(path), path
        assert classify_change([path]) == expected, path

    assert classify_change(["gateway/docs/gateway.ava.okf.md", "gateway/api.py"]) == (
        False,
        True,
    )
