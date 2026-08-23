import pytest

from shared.repo_change import classify_change, is_doc_path


@pytest.mark.parametrize(
    "path",
    [
        "decisions/foo.md",
        "postmortems/x.md",
        "future/y.md",
        "conventions/z.md",
        "okf/index.ava.okf.md",
        "assets/img.png",
        "schedules/x.json",
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
    ],
)
def test_is_doc_path(path: str) -> None:
    assert is_doc_path(path)


@pytest.mark.parametrize("path", ["ui/web/CLAUDE.md", "gateway/README.md"])
def test_is_doc_path_leaves_nested_code_docs_to_their_directory(path: str) -> None:
    assert not is_doc_path(path)


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["ui/web/app/page.tsx", "ui/web/src/lib/api.ts"], (True, False)),
        (["agent/graph/_exec.py"], (False, True)),
        (
            [
                "decisions/foo.md",
                "postmortems/x.md",
                "future/y.md",
                "conventions/z.md",
                "okf/index.ava.okf.md",
                "assets/img.png",
                "schedules/x.json",
            ],
            (False, False),
        ),
        (["README.md", "AGENTS.md", "CLAUDE.md"], (False, False)),
        (["ui/web/CLAUDE.md"], (True, False)),
        (["gateway/README.md"], (False, True)),
        ([], (False, False)),
        (["ui/web/app/page.tsx", "agent/graph/_exec.py"], (True, True)),
    ],
)
def test_classify_change(paths: list[str], expected: tuple[bool, bool]) -> None:
    assert classify_change(paths) == expected
