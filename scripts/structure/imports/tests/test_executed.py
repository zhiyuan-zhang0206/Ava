"""Actual -c launches, bounded helper inputs and retained analysis gaps."""

from __future__ import annotations

import ast

import pytest

from scripts.structure.imports import executed

_PATH = "scripts/tests/test_probe.py"


def _inputs(text: str) -> executed.Inputs:
    return executed.inputs(ast.parse(text), _PATH)


@pytest.mark.parametrize(
    "launch",
    [
        "subprocess.run([sys.executable, '-c', code])",
        "subprocess.Popen((sys.executable, '-B', '-c', code))",
        "subprocess.check_output(args=[sys.executable, '-c', code])",
        "asyncio.create_subprocess_exec(sys.executable, '-c', code)",
    ],
)
def test_single_literal_binding_reaches_real_python_argv(launch: str) -> None:
    found = _inputs(
        "import sys, subprocess, asyncio\n"
        "code = 'from base.db import pool'\n"
        "sample = 'from agent.execution import child'\n" + launch
    )
    assert [source.text for source in found.sources] == ["from base.db import pool"]
    assert found.unresolved == []


def test_aliases_and_local_scopes_keep_distinct_input_bindings() -> None:
    found = _inputs(
        "from sys import executable as python\nfrom subprocess import run as launch\n"
        "code = 'import base.db'\n"
        "def first():\n code = 'import agent.execution'\n launch([python, '-c', code])\n"
        "def second():\n launch([python, '-c', code])\n"
    )
    assert [source.text for source in found.sources] == ["import agent.execution", "import base.db"]
    assert found.unresolved == []


def test_transparent_local_helper_uses_callers_literal_or_single_binding() -> None:
    found = _inputs(
        "import sys, subprocess\n"
        "def _spawn(code, *, timeout=2):\n"
        " return subprocess.run([sys.executable, '-B', '-c', code], timeout=timeout)\n"
        "def test_driver():\n"
        " driver = \"import runpy\\nrunpy.run_module('agent.execution.child')\\n\"\n"
        " _spawn(driver)\n"
        " _spawn(code='import base.config')\n"
    )
    assert [source.text for source in found.sources] == [
        "import runpy\nrunpy.run_module('agent.execution.child')\n",
        "import base.config",
    ]
    assert found.unresolved == []


@pytest.mark.parametrize(
    "text",
    [
        "import sys\nsource = 'import base.config'\nprint(sys.executable, source)",
        "import sys, subprocess\nsource = 'import base.config'\n"
        "subprocess.run([sys.executable, '-m', 'json.tool'])",
        "import sys, subprocess\nsource = 'import base.config'\n"
        "subprocess.run([sys.executable, 'script.py', '-c', source])",
        "import sys, subprocess\nsubprocess.run([sys.executable, str(script), '--help'])",
        "import sys\nclass Fake:\n def run(self, argv): pass\n"
        "Fake().run([sys.executable, '-c', 'import base.config'])",
    ],
)
def test_non_execution_data_and_non_python_c_argv_are_not_source(text: str) -> None:
    assert _inputs(text) == executed.Inputs()


@pytest.mark.parametrize(
    "body",
    [
        "code = 'import base.config'\ncode = 'import agent.execution'\n",
        "code = make_code()\n",
        "code = f'import {module}'\n",
        "first = 'import base.config'\ncode = first\n",
    ],
)
def test_unsupported_source_values_keep_path_line_and_reason(body: str) -> None:
    text = "import sys, subprocess\n" + body + "subprocess.run([sys.executable, '-c', code])"
    found = _inputs(text)
    assert found.sources == []
    assert found.unresolved == [
        executed.Unresolved(
            _PATH, len(text.splitlines()), "Python -c source is not a literal or single binding"
        )
    ]


def test_two_hop_helper_is_not_interpreted_or_silently_complete() -> None:
    found = _inputs(
        "import sys, subprocess\n"
        "def spawn(code):\n return subprocess.run([sys.executable, '-c', code])\n"
        "def indirect(code):\n return spawn(code)\n"
        "indirect('import base.config')\n"
    )
    assert found.sources == []
    assert [(u.path, u.line) for u in found.unresolved] == [(_PATH, 5)]


@pytest.mark.parametrize(
    "argv", ["[sys.executable, flag, '-c', 'import base.config']", "[sys.executable, '-c']"]
)
def test_unknown_interpreter_options_and_missing_source_are_retained(argv: str) -> None:
    found = _inputs(f"import sys, subprocess\nsubprocess.run({argv})")
    assert found.sources == []
    assert [(u.path, u.line) for u in found.unresolved] == [(_PATH, 2)]


def test_shadowed_helper_call_cannot_make_an_unknown_template_disappear() -> None:
    found = _inputs(
        "import sys, subprocess\n"
        "def spawn(code):\n return subprocess.run([sys.executable, '-c', code])\n"
        "def test_other(spawn):\n spawn('import base.config')\n"
    )
    assert found.sources == []
    assert [(u.path, u.line, u.reason) for u in found.unresolved] == [
        (_PATH, 3, "Python -c helper input has no resolved local caller")
    ]


def test_literal_helper_default_is_not_a_missing_execution_input() -> None:
    found = _inputs(
        "import sys, subprocess\n"
        "def spawn(env, code='import base.config'):\n"
        " return subprocess.run([sys.executable, '-c', code], env=env)\n"
        "spawn({})\n"
    )
    assert [source.text for source in found.sources] == ["import base.config"]
    assert found.unresolved == []


def test_lambda_parameter_does_not_inherit_a_module_source_binding() -> None:
    found = _inputs(
        "import sys, subprocess\ncode = 'import base.config'\n"
        "launch = lambda code: subprocess.run([sys.executable, '-c', code])\n"
    )
    assert found.sources == []
    assert [(u.path, u.line) for u in found.unresolved] == [(_PATH, 3)]


def test_aliased_dynamic_imports_match_python_execution(pytester: pytest.Pytester) -> None:
    package = pytester.path / "base"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "runner.py").write_text("print('actual-child')\n", encoding="utf-8")
    source = (
        "from runpy import run_module as execute\n"
        "import importlib as loader\n"
        "execute('base.runner')\nloader.import_module('base.runner')\n"
    )
    child = pytester.runpython_c(source)
    assert child.ret == 0
    assert child.outlines == ["actual-child", "actual-child"]
    facts = executed.import_facts(executed.Source(7, source), _PATH)
    assert facts.targets == ["base.runner", "base.runner"]
    assert facts.unresolved == []


@pytest.mark.parametrize(
    "source",
    ["import runpy\nrunpy.run_module(module)", "from . import member", "import !broken"],
)
def test_unresolved_embedded_targets_and_invalid_source_remain_explicit(source: str) -> None:
    facts = executed.import_facts(executed.Source(14, source), _PATH)
    assert [(u.path, u.line) for u in facts.unresolved] == [(_PATH, 14)]


def test_unrelated_methods_and_shadowed_dynamic_names_are_not_imports() -> None:
    facts = executed.import_facts(
        executed.Source(
            4, "class Fake:\n def run_module(self, name): pass\nFake().run_module('base.config')\n"
        ),
        _PATH,
    )
    assert facts.targets == []
    assert facts.unresolved == []


def test_method_bodies_use_python_lexical_imports_not_class_attributes() -> None:
    source = (
        "import runpy\nclass Holder:\n runpy = None\n"
        " def execute(self):\n  runpy.run_module('base.config')\n"
    )
    facts = executed.import_facts(executed.Source(4, source), _PATH)
    assert facts.targets == ["base.config"]
    assert facts.unresolved == []


@pytest.mark.parametrize(
    "argv",
    [
        "[sys.executable, '-X', 'utf8', '-c', 'import base.config']",
        "[sys.executable, '-W', '-c', 'import base.config']",
    ],
)
def test_interpreter_option_operands_do_not_silently_erase_or_misidentify_source(argv: str) -> None:
    found = _inputs(f"import sys, subprocess\nsubprocess.run({argv})")
    assert found.sources == []
    assert found.unresolved == [
        executed.Unresolved(_PATH, 2, "Python interpreter option operands are not resolved")
    ]
