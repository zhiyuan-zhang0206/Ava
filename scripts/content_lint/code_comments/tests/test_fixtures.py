from pathlib import Path

import pytest

from scripts.content_lint.code_comments.check import check
from scripts.content_lint.code_comments.tests.cases import repository

HELP = '    """Own the temporary resource until fixture teardown."""\n'


@pytest.mark.parametrize(
    ("imports", "decorator"),
    [
        ("import pytest", "pytest.fixture"),
        ("import pytest as testing", "testing.fixture(scope='module')"),
        ("from pytest import fixture", "fixture"),
        ("from pytest import fixture as resource", "resource(name='client')"),
    ],
)
@pytest.mark.parametrize("keyword", ["def", "async def"])
def test_native_fixture_help_is_not_internal_prose(
    tmp_path: Path, imports: str, decorator: str, keyword: str
) -> None:
    source = f"{imports}\n@{decorator}\n{keyword} client():\n{HELP}    yield 1\n"
    repo = repository(tmp_path, {"pkg/tests/test_client.py": source})
    assert not check(repo, strict_paths=repo.files).findings


@pytest.mark.parametrize(
    "binding",
    [
        "pytest = replacement",
        "del pytest",
        "pytest.fixture = replacement",
        "del pytest.fixture",
        "setattr(pytest, 'fixture', replacement)",
        "from another import *",
        "for pytest in replacements:\n    pass",
        "with replacement as pytest:\n    pass",
        "try:\n    call()\nexcept ValueError as pytest:\n    pass",
        "match value:\n    case pytest:\n        pass",
    ],
)
def test_fixture_help_requires_unambiguous_import_identity(tmp_path: Path, binding: str) -> None:
    source = f"import pytest\n{binding}\n@pytest.fixture\ndef client():\n{HELP}    yield 1\n"
    repo = repository(tmp_path, {"pkg/tests/test_client.py": source})
    assert any(f.code == "free-comment" for f in check(repo, strict_paths=repo.files).findings)


@pytest.mark.parametrize(
    "decorators",
    ["@other.fixture", "@fixture", "@wrap\n@pytest.fixture", "@pytest.fixture\n@wrap"],
)
def test_unknown_decorators_do_not_authorize_help(tmp_path: Path, decorators: str) -> None:
    source = f"import pytest\n{decorators}\ndef client():\n{HELP}    yield 1\n"
    repo = repository(tmp_path, {"pkg/tests/test_client.py": source})
    assert any(f.code == "free-comment" for f in check(repo, strict_paths=repo.files).findings)


def test_fixture_identity_does_not_authorize_body_prose(tmp_path: Path) -> None:
    source = (
        "import pytest\n@pytest.fixture\ndef client():\n"
        + HELP
        + '    # unrelated prose\n    def helper():\n        """Internal notes."""\n        return 1\n    yield helper()\n'
    )
    repo = repository(tmp_path, {"pkg/tests/test_client.py": source})
    findings = check(repo, strict_paths=repo.files).findings
    assert [f.code for f in findings] == ["free-comment", "free-comment"]


@pytest.mark.parametrize("shadow", [False, True])
def test_class_fixture_uses_its_decorator_scope(tmp_path: Path, shadow: bool) -> None:
    source = "import pytest\nclass TestClient:\n"
    if shadow:
        source += "    pytest = replacement\n"
    source += "    @pytest.fixture\n    def client(self):\n    " + HELP + "        yield 1\n"
    repo = repository(tmp_path, {"pkg/tests/test_client.py": source})
    assert bool(check(repo, strict_paths=repo.files).findings) is shadow


@pytest.mark.parametrize(
    "replacement",
    ["client = replacement", "def client():\n" + HELP + "    return 2", "del client"],
)
def test_replaced_fixture_binding_does_not_authorize_help(tmp_path: Path, replacement: str) -> None:
    source = (
        "import pytest\n@pytest.fixture\ndef client():\n"
        + HELP
        + "    yield 1\n"
        + replacement
        + "\n"
    )
    repo = repository(tmp_path, {"pkg/tests/test_client.py": source})
    assert any(f.code == "free-comment" for f in check(repo, strict_paths=repo.files).findings)


@pytest.mark.parametrize("prefix", ["@replace\n", "class TestClient: pass\n"])
def test_unproved_class_identity_does_not_authorize_fixture_help(
    tmp_path: Path, prefix: str
) -> None:
    source = "import pytest\n" + prefix + "class TestClient:\n"
    source += "    @pytest.fixture\n    def client(self):\n    " + HELP + "        yield 1\n"
    repo = repository(tmp_path, {"pkg/tests/test_client.py": source})
    assert any(f.code == "free-comment" for f in check(repo, strict_paths=repo.files).findings)


@pytest.mark.parametrize(
    ("imports", "decorator"),
    [("import pytest", "pytest.fixture"), ("from pytest import fixture as resource", "resource")],
)
@pytest.mark.parametrize("in_class", [False, True])
def test_late_import_does_not_prove_decorator_identity(
    tmp_path: Path, imports: str, decorator: str, in_class: bool
) -> None:
    source = f"@{decorator}\ndef client():\n{HELP}    yield 1\n"
    if in_class:
        source = "class TestClient:\n" + "".join(
            "    " + line for line in source.splitlines(keepends=True)
        )
    source += imports + "\n"
    repo = repository(tmp_path, {"pkg/tests/test_client.py": source})
    assert any(f.code == "free-comment" for f in check(repo, strict_paths=repo.files).findings)


@pytest.mark.parametrize(
    "declaration",
    [
        "class Configure:\n    pytest.fixture = replacement",
        "class Configure:\n    del pytest.fixture",
        "class Configure:\n    global pytest\n    pytest = replacement",
        "class Configure:\n    import pytest as local_testing\n    local_testing.fixture = replacement",
        "def configure(value=(pytest := replacement)): pass",
        "@(pytest := replacement)\ndef configure(): pass",
        "class Configure((pytest := replacement)): pass",
    ],
)
def test_declaration_time_writes_invalidate_fixture_proof(tmp_path: Path, declaration: str) -> None:
    source = f"import pytest\n{declaration}\n@pytest.fixture\ndef client():\n{HELP}    yield 1\n"
    repo = repository(tmp_path, {"pkg/tests/test_client.py": source})
    assert any(f.code == "free-comment" for f in check(repo, strict_paths=repo.files).findings)


def test_class_local_name_does_not_shadow_the_module_import(tmp_path: Path) -> None:
    source = "import pytest\nclass Configure:\n    pytest = replacement\n"
    source += "@pytest.fixture\ndef client():\n" + HELP + "    yield 1\n"
    repo = repository(tmp_path, {"pkg/tests/test_client.py": source})
    assert not check(repo, strict_paths=repo.files).findings
