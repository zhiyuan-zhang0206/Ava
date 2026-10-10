"""Actual -c launches, bounded helper inputs and retained analysis gaps."""

from __future__ import annotations

import ast
import sys

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
        "asyncio.create_subprocess_exec(sys.executable, '-X', 'utf8', '-c', code, *data)",
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
        "[sys.executable, '-I', '-X', 'utf8', '-c', source, *data]",
        "[sys.executable, '-W', 'ignore', '-X', 'utf8', '-c', source, dynamic]",
        "[sys.executable, '-W', '-c', '-c', source]",
        "[sys.executable, '-Xutf8', '-Wignore', '-c', source]",
    ],
)
def test_literal_interpreter_operands_preserve_source_and_ignore_argv_data(argv: str) -> None:
    found = _inputs(
        f"import sys, subprocess\nsource = 'import base.config'\nsubprocess.run({argv})"
    )
    assert [source.text for source in found.sources] == ["import base.config"]
    assert found.unresolved == []


@pytest.mark.parametrize("operand", ["dynamic", "*options", "make_option()"])
def test_unknown_option_operand_still_retains_a_gap(operand: str) -> None:
    found = _inputs(
        "import sys, subprocess\n"
        f"subprocess.run([sys.executable, '-X', {operand}, '-c', 'import base.config'])"
    )
    assert found.sources == []
    assert found.unresolved == [
        executed.Unresolved(_PATH, 2, "Python interpreter option operands are not literal")
    ]


def test_option_operand_named_c_is_data_not_the_command_selector() -> None:
    found = _inputs(
        "import sys, subprocess\n"
        "subprocess.run([sys.executable, '-W', '-c', 'script.py', 'import base.config'])"
    )
    assert found == executed.Inputs()


def test_one_helper_binding_and_trailing_starred_data_preserve_source() -> None:
    found = _inputs(
        "import sys, subprocess\n"
        "def spawn(code, *data):\n"
        " source = code\n"
        " return subprocess.run([sys.executable, '-c', source, *data])\n"
        "def test_driver(data):\n"
        " source = 'import base.config'\n"
        " spawn(source, *data)\n"
    )
    assert [source.text for source in found.sources] == ["import base.config"]
    assert found.unresolved == []


def test_keyword_only_helper_source_is_independent_of_starred_argv_data() -> None:
    found = _inputs(
        "import sys, subprocess\n"
        "def spawn(*data, code):\n"
        " return subprocess.run([sys.executable, '-c', code, *data])\n"
        "spawn(*data, code='import base.config')\n"
    )
    assert [source.text for source in found.sources] == ["import base.config"]
    assert found.unresolved == []


@pytest.mark.parametrize(
    "call",
    [
        "spawn(*data)",
        "spawn(*data, code='import base.config')",
        "spawn('import base.config', **options)",
        "spawn('import base.config', code='import agent.child')",
    ],
)
def test_ambiguous_helper_source_slot_does_not_become_complete(call: str) -> None:
    found = _inputs(
        "import sys, subprocess\n"
        "def spawn(code, *data):\n"
        " return subprocess.run([sys.executable, '-c', code, *data])\n" + call
    )
    assert found.sources == []
    assert [(gap.path, gap.line) for gap in found.unresolved] == [(_PATH, 4)]


@pytest.mark.parametrize(
    "body", ["source = code\n source = build()", "first = code\n source = first"]
)
def test_rebound_or_two_binding_helper_source_stays_unknown(body: str) -> None:
    found = _inputs(
        "import sys, subprocess\n"
        "def spawn(code):\n " + body + "\n"
        " return subprocess.run([sys.executable, '-c', source])\n"
        "spawn('import base.config')\n"
    )
    assert found.sources == []
    assert len(found.unresolved) == 1


def test_known_option_and_argv_data_match_python_execution(pytester: pytest.Pytester) -> None:
    source = "import sys\nprint(sys.argv[1])\n"
    child = pytester.run(sys.executable, "-X", "utf8", "-W", "ignore", "-c", source, "payload")
    assert child.ret == 0
    assert child.outlines == ["payload"]
    found = _inputs(
        "import sys, subprocess\n"
        f"subprocess.run([sys.executable, '-X', 'utf8', '-W', 'ignore', '-c', {source!r}, data])"
    )
    assert [item.text for item in found.sources] == [source]
    assert found.unresolved == []


def test_keyword_only_literal_default_does_not_depend_on_starred_data() -> None:
    found = _inputs(
        "import sys, subprocess\n"
        "def spawn(*data, code='import base.config'):\n"
        " return subprocess.run([sys.executable, '-c', code, *data])\n"
        "spawn(*data)\n"
    )
    assert [source.text for source in found.sources] == ["import base.config"]
    assert found.unresolved == []


def test_repeated_helper_callers_keep_sources_and_opaque_arguments_independent() -> None:
    found = _inputs(
        "import sys, subprocess\n"
        "def spawn(code):\n return subprocess.run([sys.executable, '-c', code])\n"
        "spawn('import base.config')\n"
        "spawn(opaque)\n"
        "spawn('import agent.db')\n"
    )
    assert [(source.line, source.text) for source in found.sources] == [
        (4, "import base.config"),
        (6, "import agent.db"),
    ]
    assert [(gap.line, gap.reason) for gap in found.unresolved] == [
        (5, "Python -c source is not a literal or single binding")
    ]
