from __future__ import annotations

from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, edges

LIB = {
    "lib/__init__.py": "",
    "lib/__main__.py": "",
    "lib/tool.py": "",
    "app/__init__.py": "",
    "scripts/run.py": "",
}
HEAD = "import subprocess\nimport sys\nfrom pathlib import Path\n\n"


def check(make_repo: RepoFactory, body: str, *, more: dict[str, str] | None = None) -> Path:
    return make_repo({**LIB, **(more or {}), "app/x.py": HEAD + body})


def restricted_of(root: Path) -> list[str]:
    return [d.message for d in diagnostics(root, "restricted-python") if d.path == "app/x.py"]


def loads_of(root: Path) -> set[tuple[str, str]]:
    return {(src, dst) for src, dst, _ in edges(root) if src == "app/x.py" and dst != "app/x.py"}


def restricted_forms(make_repo: RepoFactory, body: str) -> list[str]:
    return restricted_of(check(make_repo, body))


def loads(make_repo: RepoFactory, body: str) -> set[tuple[str, str]]:
    return loads_of(check(make_repo, body))


@pytest.mark.parametrize(
    "command",
    [
        '[sys.executable, "-c", "pass"]',
        '[sys.executable, "-I", "-B", "-X", "utf8", "-c", "import lib"]',
        '[sys.executable, "-u", "-c", code]',
        '["/usr/bin/python3", "-c", "pass"]',
        '[py, "-c", "pass"]',
    ],
)
def test_dash_c_is_restricted_whatever_it_runs(make_repo: RepoFactory, command: str) -> None:
    body = f"py = sys.executable\ncode = 'pass'\nsubprocess.run({command})\n"
    assert restricted_forms(make_repo, body) == [
        "python -c is outside the restricted Python subset"
    ]


@pytest.mark.parametrize(
    ("command", "form"),
    [
        ('[sys.executable, "-"]', "python source on standard input"),
        (
            "[sys.executable, str(tmp / 'x.py')]",
            "python subprocess that runs something other than a literal repository file",
        ),
        (
            "[sys.executable, script]",
            "python subprocess that runs something other than a literal repository file",
        ),
        (
            "[sys.executable, f'{tmp}/x.py']",
            "python subprocess that runs something other than a literal repository file",
        ),
        ("[sys.executable, *rest]", "python subprocess with arguments built with *"),
        ("[sys.executable, '-m', module]", "python -m of a module that is not a literal"),
    ],
)
def test_what_the_checker_cannot_see_is_restricted(
    make_repo: RepoFactory, command: str, form: str
) -> None:
    body = f"def go(tmp, script, rest, module):\n    subprocess.run({command})\n"
    assert restricted_forms(make_repo, body) == [f"{form} is outside the restricted Python subset"]


@pytest.mark.parametrize(
    "command",
    [
        '[sys.executable, "-m", "lib.tool", "--flag"]',
        '[sys.executable, "-I", "-m", "lib.tool"]',
        '(sys.executable, "-m", "lib.tool")',
    ],
)
def test_dash_m_of_a_first_party_module_is_an_edge_to_its_file(
    make_repo: RepoFactory, command: str
) -> None:
    root = check(make_repo, f"subprocess.run({command})\n")
    assert restricted_of(root) == []
    assert loads_of(root) == {("app/x.py", "lib/tool.py")}


def test_dash_m_of_a_package_runs_its_door_and_its_main(make_repo: RepoFactory) -> None:
    root = check(make_repo, 'subprocess.run([sys.executable, "-m", "lib"])\n')
    assert loads_of(root) == {("app/x.py", "lib/__init__.py"), ("app/x.py", "lib/__main__.py")}


def test_a_dunder_module_is_protocol_not_private(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            **LIB,
            "other/__init__.py": "",
            "other/__main__.py": "",
            "app/x.py": HEAD + 'subprocess.run([sys.executable, "-m", "other"])\n',
        }
    )
    found = {d.code.value for d in analysis_of(root).diagnostics if d.path == "app/x.py"}
    assert "private-module" not in found


@pytest.mark.parametrize(
    "call",
    [
        'run_bounded(["python3", "-c", "pass"], timeout=5)',
        'subprocess.check_output(["python", "-c", "pass"])',
        'os.execv("/usr/bin/python3", ["python3", "-c", "pass"])',
        'os.execvp("python", ["python", "-c", "pass"])',
        'os.execl(sys.executable, sys.executable, "-c", "pass")',
        'os.execlp("python", "python", "-c", "pass")',
    ],
)
def test_the_calls_that_start_a_process_are_judged(make_repo: RepoFactory, call: str) -> None:
    body = f"import os\nfrom base.host.proc import run_bounded\n\ndef go():\n    {call}\n"
    root = make_repo(
        {
            **LIB,
            "base/__init__.py": "",
            "base/host/__init__.py": "",
            "base/host/proc.py": "def run_bounded(argv, *, timeout): ...\n",
            "app/x.py": HEAD + body,
        }
    )
    assert restricted_of(root) == ["python -c is outside the restricted Python subset"]


def test_a_shell_string_through_asyncio_is_judged_by_what_it_imports(
    make_repo: RepoFactory,
) -> None:
    body = (
        "import asyncio\n\n"
        "async def go():\n"
        "    await asyncio.create_subprocess_shell(\"python -c 'import lib'\")\n"
    )
    root = check(make_repo, body)
    assert restricted_of(root) == [
        "python -c source that imports first-party code is outside the restricted Python subset"
    ]


def test_dash_m_of_a_third_party_module_is_no_edge(make_repo: RepoFactory) -> None:
    root = check(make_repo, 'subprocess.run([sys.executable, "-m", "pytest", "-q"])\n')
    assert restricted_of(root) == []
    assert loads_of(root) == set()


def test_dash_m_of_a_missing_first_party_module_is_an_error(make_repo: RepoFactory) -> None:
    root = check(make_repo, 'subprocess.run([sys.executable, "-m", "lib.nope"])\n')
    [found] = diagnostics(root, "unresolved-import")
    assert "names no tracked module" in found.message


@pytest.mark.parametrize(
    "target",
    [
        '"scripts/run.py"',
        'Path("scripts/run.py")',
        'str(Path(__file__).parent.parent / "scripts" / "run.py")',
        'str(Path(__file__).resolve().parents[1] / "scripts/run.py")',
        'str(Path(__file__).parent.with_name("scripts") / "run.py")',
        "str(SCRIPT)",
        "str(ROOT / 'scripts' / 'run.py')",
    ],
)
def test_a_literal_repository_file_is_an_edge(make_repo: RepoFactory, target: str) -> None:
    body = (
        'SCRIPT = Path(__file__).parent.parent / "scripts" / "run.py"\n'
        "ROOT = Path(__file__).resolve().parents[1]\n"
        f"subprocess.run([sys.executable, {target}, '--x'])\n"
    )
    root = check(make_repo, body)
    assert restricted_of(root) == []
    assert loads_of(root) == {("app/x.py", "scripts/run.py")}


def test_a_sibling_file_through_with_name_is_an_edge(make_repo: RepoFactory) -> None:
    body = 'subprocess.run([sys.executable, str(Path(__file__).with_name("child.py"))])\n'
    root = make_repo({**LIB, "app/child.py": "", "app/x.py": HEAD + body})
    assert ("app/x.py", "app/child.py") in {(s, d) for s, d, _ in edges(root)}


def test_a_file_that_starts_itself_again_depends_on_nothing_new(make_repo: RepoFactory) -> None:
    root = check(make_repo, 'subprocess.run([sys.executable, __file__, "child"])\n')
    assert restricted_of(root) == []
    assert loads_of(root) == set()


def test_a_literal_path_to_nothing_is_an_error(make_repo: RepoFactory) -> None:
    root = check(make_repo, 'subprocess.run([sys.executable, "scripts/gone.py"])\n')
    [found] = diagnostics(root, "unresolved-import")
    assert "scripts/gone.py" in found.message


def test_a_first_party_modules_file_attribute_is_its_file(make_repo: RepoFactory) -> None:
    root = check(
        make_repo, "from lib import tool\nsubprocess.run([sys.executable, tool.__file__])\n"
    )
    assert restricted_of(root) == []
    assert ("app/x.py", "lib/tool.py") in loads_of(root)


def test_a_bare_python_is_a_command_only_where_a_process_is_spawned(
    make_repo: RepoFactory,
) -> None:
    body = (
        '("python", "Python (uv audit)")\n'
        'fake = ["python", "-c", "print(1)"]\n'
        'labels = ["python", "lib.tool"]\n'
        'subprocess.run(["python3", "-c", "print(1)"])\n'
    )
    assert restricted_forms(make_repo, body) == [
        "python -c is outside the restricted Python subset"
    ]


def test_a_spawned_process_through_asyncio_is_judged_too(make_repo: RepoFactory) -> None:
    body = (
        "import asyncio\n\n"
        "async def go():\n"
        '    await asyncio.create_subprocess_exec(sys.executable, "-m", "lib.tool")\n'
        '    await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")\n'
    )
    root = check(make_repo, body)
    assert restricted_of(root) == ["python -c is outside the restricted Python subset"]
    assert ("app/x.py", "lib/tool.py") in loads_of(root)


def test_a_shell_string_with_dash_c_is_still_judged_by_what_it_imports(
    make_repo: RepoFactory,
) -> None:
    body = "subprocess.run(\"python -c 'import lib'\", shell=True)\n"
    assert restricted_forms(make_repo, body) == [
        "python -c source that imports first-party code is outside the restricted Python subset"
    ]


def test_a_command_edge_is_judged_like_an_import(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            **LIB,
            "other/__init__.py": "",
            "other/child.py": "",
            "app/x.py": HEAD + 'subprocess.run([sys.executable, "other/child.py"])\n',
        }
    )
    assert {d.code.value for d in analysis_of(root).diagnostics if d.path == "app/x.py"} <= {
        "undeclared-edge",
        "not-exported",
    }
