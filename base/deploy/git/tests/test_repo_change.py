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
        (["ui/web/app/page.tsx", "ui/web/src/lib/api.ts"], (True, False)),
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
