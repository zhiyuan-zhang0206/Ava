from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.telemetry import EventPipeline
from cli.commands.management import schedules_verify as _verify
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

_OLD_HIERARCHY = """\
import time
from datetime import UTC, datetime

from schedules.catchup import cluster_timezone, fire_slot_once
from base.daemon.schedules.watcher import next_fire

CRON = "* * * * *"


def _fire(_trigger):
    pass


def main():
    nxt = next_fire(CRON, after=datetime.now(UTC), timezone=cluster_timezone())
    fire_slot_once(nxt, None, fire=_fire)
"""

_NEW_HIERARCHY = (
    _OLD_HIERARCHY.replace("def _fire(_trigger):", "def _fire(_slot, _trigger):")
    .replace("fire_slot_once(nxt", "fire_slot_once(db, nxt")
    .replace("def main():\n", "def main():\n    db = None\n")
)


def _check(script: str) -> str | None:
    return _verify.check_script(script)


def test_an_old_signature_call_is_red_where_the_import_alone_was_green() -> None:
    red = _check(_OLD_HIERARCHY)
    assert red is not None and red.startswith("call-signature:")
    assert "fire_slot_once(): missing a required argument: 'payload'" in red


def test_the_current_signature_is_green() -> None:
    assert _check(_NEW_HIERARCHY) is None


def test_the_attribute_form_and_an_alias_are_resolved() -> None:
    script = (
        "import schedules.catchup as c\nimport schedules.catchup\n"
        "c.fire_slot_once(1, fire=print)\n"
        "schedules.catchup.fire_slot_once(1, 2, fire=print)\n"
    )
    red = _check(script)
    assert red is not None
    assert "L3 c.fire_slot_once()" in red and "L4 schedules.catchup.fire_slot_once()" in red


def test_an_unknown_keyword_is_red() -> None:
    red = _check(
        "from schedules.catchup import fire_slot_once\nfire_slot_once(1, 2, 3, fire=print, nope=1)\n"
    )
    assert red is not None and "unexpected keyword argument 'nope'" in red


def test_a_removed_module_attribute_is_red() -> None:
    red = _check("import schedules.catchup\nschedules.catchup.gone_function(1)\n")
    assert red is not None and "schedules.catchup has no `gone_function`" in red


@pytest.mark.parametrize(
    "script",
    [
        "from schedules.catchup import fire_slot_once\n\na, k = (), {}\nfire_slot_once(*a, **k)\n",
        "from schedules.catchup import fire_slot_once\n\ndef fire_slot_once(): ...\n\nfire_slot_once()\n",
        "from schedules.catchup import fire_slot_once\n\ndef f(fire_slot_once):\n    fire_slot_once()\n",
        "from base.db import Database\nfrom base.db.code_version_gate import ProcessDbGate\n"
        "def connect(gate: ProcessDbGate):\n"
        "    Database.from_settings(gate=gate).connect()\n",
        "import meiva\nmeiva.agents.spawn(prompt='x', label='plugin-added keyword')\n",
        "import meiva\nresults = meiva.tasks.get('x').results\n",
        "import meiva\n\ndef scan(task_id):\n    return meiva.tasks.get(task_id).results\n",
        "import json\njson.dumps()\n",
        "import os\nos.getpid(1)\n",
    ],
)
def test_calls_it_cannot_prove_wrong_are_not_red(script: str) -> None:
    assert _check(script) is None


def test_a_class_constructor_is_checked_too() -> None:
    red = _check("from base.db import Database\nDatabase(1, 2, 3, 4, 5, 6)\n")
    assert red is not None and "Database()" in red


def test_check_file_prints_the_call_signature_verdict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    stale = tmp_path / "stale.py"
    stale.write_text(_OLD_HIERARCHY, encoding="utf-8")
    assert (
        _verify.cmd_schedules_verify(
            check_file=str(stale), database_factory=operator_database, producer=operator_pipeline
        )
        == 1
    )
    assert "CHECK-RED missing=call-signature:L16 fire_slot_once()" in capsys.readouterr().out


def test_the_sweep_checks_agent_written_rows_like_built_in_ones(
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    rows = [(1, "built-in", _NEW_HIERARCHY), (9, "agent-made-poller", _OLD_HIERARCHY)]

    def no_alert(**_kwargs: object) -> None:
        return None

    ports = _verify.VerifyPorts(
        read_rows=lambda: rows, check_script=_verify.check_script, report=no_alert
    )
    assert (
        _verify.cmd_schedules_verify(
            notify=False,
            ports=ports,
            database_factory=operator_database,
            producer=operator_pipeline,
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "checked=2 green=1 red=1 rc=1" in out
    assert "RED id=9 name=agent-made-poller missing=call-signature:" in out


def test_every_shipped_built_in_script_binds_against_the_library() -> None:
    manifest = json.loads(
        (Path(__file__).resolve().parents[4] / "schedules" / "manifest.json").read_text()
    )
    reds = {}
    for entry in manifest["builtin_schedules"]:
        script = (Path(__file__).resolve().parents[4] / "schedules" / entry["script"]).read_text()
        if red := _check(script):
            reds[entry["name"]] = red
    assert reds == {}


def test_a_read_of_a_name_nothing_imported_is_red() -> None:
    red = _check("from base.config import settings\nroot = base.__file__\n")
    assert red is not None and red.startswith("undefined-name:")
    assert "L2 base" in red


def test_a_read_inside_a_function_is_red_too() -> None:
    red = _check("from base.config import settings\n\n\ndef run():\n    return base.__file__\n")
    assert red is not None and red.startswith("undefined-name:") and "L5 base" in red


def test_names_the_script_binds_or_the_language_provides_are_green() -> None:
    assert (
        _check(
            "import base.config\nfrom typing import Any\n\n"
            "if __name__ == '__main__':\n"
            "    value: Any = None\n"
            "    items = [v for v in range(3)]\n"
            "    print(__file__, __name__, value, items, base.config.__name__)\n"
        )
        is None
    )


def test_annotations_are_not_read() -> None:
    assert _check("def f(value: zz_missing_type) -> zz_missing_return:\n    return value\n") is None


@pytest.mark.parametrize(
    "script",
    [
        "def identity[T](value):\n    return T\n",
        "def wrap[**P](fn):\n    return P\n",
        "def pack[*Ts](values):\n    return Ts\n",
        "class Bag[T]:\n    item = T\n",
        "class BagP[**P]:\n    item = P\n",
        "class BagT[*Ts]:\n    item = Ts\n",
        "type Bag[T] = list[T]\n",
        "type BagP[**P] = list[P]\n",
        "type BagTs[*Ts] = list[*Ts]\n",
    ],
    ids=[
        "fn-typevar",
        "fn-paramspec",
        "fn-typevartuple",
        "class-typevar",
        "class-paramspec",
        "class-typevartuple",
        "alias-typevar",
        "alias-paramspec",
        "alias-typevartuple",
    ],
)
def test_pep695_type_parameters_are_bindings(script: str) -> None:
    assert _check(script) is None


@pytest.mark.parametrize(
    "script",
    [
        "def f[T: zz_missing_bound]():\n    return T\n",
        "class C[T: zz_missing_bound]:\n    pass\n",
        "type A[T: zz_missing_bound] = list[T]\n",
    ],
    ids=["fn", "class", "alias"],
)
def test_type_parameter_bounds_are_not_read(script: str) -> None:
    assert _check(script) is None


def test_a_missing_name_next_to_a_bound_type_param_is_still_red() -> None:
    red = _check("def identity[T](value: T) -> T:\n    return zz_missing_value\n")
    assert red is not None and red.startswith("undefined-name:")
    assert "L2 zz_missing_value" in red


def test_a_missing_name_in_a_type_alias_value_is_still_red() -> None:
    red = _check("type Bag[T] = list[zz_missing_alias]\n")
    assert red is not None and red.startswith("undefined-name:") and "L1 zz_missing_alias" in red


def test_a_conditional_import_binding_is_green() -> None:
    assert (
        _check(
            "try:\n    import zz_maybe\nexcept ImportError:\n    zz_maybe = None\nprint(zz_maybe)\n"
        )
        is None
    )


def test_a_star_import_disables_the_check() -> None:
    assert _check("from os.path import *\nprint(zz_whatever_from_the_star)\n") is None


def test_check_file_prints_the_undefined_name_verdict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    stale = tmp_path / "stale.py"
    stale.write_text("from base.config import settings\nroot = base.__file__\n", encoding="utf-8")
    assert (
        _verify.cmd_schedules_verify(
            check_file=str(stale), database_factory=operator_database, producer=operator_pipeline
        )
        == 1
    )
    assert "CHECK-RED missing=undefined-name:L2 base" in capsys.readouterr().out
