"""`ava schedules verify`'s call-site check — the signature drift the import check cannot see.

The 2026-10-03 shape: `schedules.catchup.catch_up()` / `fire_slot_once()` gained a required
leading `db` and the hierarchy-worker's `prepare` moved; the stored scripts still imported cleanly
(every name still existed) and crash-looped on their first call. These run the real child
(`schedule_verify_child`) the way the sweep does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.commands.management import schedules_verify as _verify

# The hierarchy-worker script as stored before the library layer took `db` from its caller.
_OLD_HIERARCHY = """\
import time
from datetime import UTC, datetime

from schedules.catchup import catch_up, cluster_timezone, fire_slot_once
from base.daemon.schedules.watcher import next_fire

CRON = "* * * * *"


def _fire(_trigger):
    pass


def main():
    catch_up([(CRON, None)], timezone=cluster_timezone(), fire=_fire)
    nxt = next_fire(CRON, after=datetime.now(UTC), timezone=cluster_timezone())
    fire_slot_once(nxt, None, fire=_fire)
"""

# The current shape receives `db` and binds it (the shipped templates do
# `db = Database.from_settings()`); an unbound read is its own red — see the undefined-name tests.
_NEW_HIERARCHY = (
    _OLD_HIERARCHY.replace("catch_up([", "catch_up(db, [")
    .replace("fire_slot_once(nxt", "fire_slot_once(db, nxt")
    .replace("def main():\n", "def main():\n    db = None\n")
)


def _check(script: str) -> str | None:
    return _verify._check_script(script)


def test_an_old_signature_call_is_red_where_the_import_alone_was_green() -> None:
    red = _check(_OLD_HIERARCHY)
    assert red is not None and red.startswith("call-signature:")
    assert "L15 catch_up(): missing a required argument: 'triggers'" in red
    assert "fire_slot_once(): missing a required argument: 'payload'" in red


def test_the_current_signature_is_green() -> None:
    assert _check(_NEW_HIERARCHY) is None


def test_the_attribute_form_and_an_alias_are_resolved() -> None:
    script = (
        "import schedules.catchup as c\nimport schedules.catchup\n"
        "c.catch_up([], timezone=None, fire=print)\n"
        "schedules.catchup.fire_slot_once(1, 2, fire=print)\n"
    )
    red = _check(script)
    assert red is not None
    assert "L3 c.catch_up()" in red and "L4 schedules.catchup.fire_slot_once()" in red


def test_an_unknown_keyword_is_red() -> None:
    red = _check(
        "from schedules.catchup import catch_up\ncatch_up(1, [], timezone=None, fire=print, nope=1)\n"
    )
    assert red is not None and "unexpected keyword argument 'nope'" in red


def test_a_removed_module_attribute_is_red() -> None:
    red = _check("import schedules.catchup\nschedules.catchup.gone_function(1)\n")
    assert red is not None and "schedules.catchup has no `gone_function`" in red


@pytest.mark.parametrize(
    "script",
    [
        # Unbindable statically: starred / double-starred arguments.
        "from schedules.catchup import catch_up\n\na, k = (), {}\ncatch_up(*a, **k)\n",
        # A name the script rebinds is not the imported object.
        "from schedules.catchup import catch_up\n\ndef catch_up(): ...\n\ncatch_up()\n",
        "from schedules.catchup import catch_up\n\ndef f(catch_up):\n    catch_up()\n",
        # A callee reached through an instance cannot be resolved without running anything.
        "from base.db import Database\nDatabase.from_settings().connect()\n",
        # The agent SDK is wrapped by plugins at load time, so its static signature is not the contract.
        "import ava\nava.agents.spawn(prompt='x', label='plugin-added keyword')\n",
        # A namespace a plugin installs into `ava` at load time (2026-10-04, model-scan-backstop).
        "import ava\nresults = ava.tasks.get('x').results\n",
        "import ava\n\ndef scan(task_id):\n    return ava.tasks.get(task_id).results\n",
        # Code outside the checkout is not this check's business.
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
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "stale.py"
    stale.write_text(_OLD_HIERARCHY, encoding="utf-8")
    assert _verify.cmd_schedules_verify(check_file=str(stale)) == 1
    assert "CHECK-RED missing=call-signature:L15 catch_up()" in capsys.readouterr().out


def test_the_sweep_checks_agent_written_rows_like_built_in_ones(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The sweep reads every row of the table; a script an agent wrote is no exception."""
    rows = [(1, "built-in", _NEW_HIERARCHY), (9, "agent-made-poller", _OLD_HIERARCHY)]

    def no_alert(**_kwargs: object) -> None:
        return None

    ports = _verify.VerifyPorts(
        read_rows=lambda: rows, check_script=_verify._check_script, report=no_alert
    )
    assert _verify.cmd_schedules_verify(notify=False, ports=ports) == 1
    out = capsys.readouterr().out
    assert "checked=2 green=1 red=1 rc=1" in out
    assert "RED id=9 name=agent-made-poller missing=call-signature:" in out


def test_every_shipped_built_in_script_binds_against_the_library() -> None:
    """A signature change in a library module must update the templates in the same PR."""
    manifest = json.loads(
        (Path(__file__).resolve().parents[4] / "schedules" / "manifest.json").read_text()
    )
    reds = {}
    for entry in manifest["builtin_schedules"]:
        script = (Path(__file__).resolve().parents[4] / "schedules" / entry["script"]).read_text()
        if red := _check(script):
            reds[entry["name"]] = red
    assert reds == {}


# ── the undefined-name check: the read the import dry-run cannot see ──
# 2026-10-01 audit: three in-store copies read `base` where nothing imported it (shared -> base
# rename leftovers at a non-import top-level statement); the import check stayed green and the
# first real fire died with NameError.


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
    """A type parameter is a name the script binds (2026-10-05 review); reading it is not drift."""
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
    """A bound is evaluated lazily like an annotation, never at fire time."""
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
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "stale.py"
    stale.write_text("from base.config import settings\nroot = base.__file__\n", encoding="utf-8")
    assert _verify.cmd_schedules_verify(check_file=str(stale)) == 1
    assert "CHECK-RED missing=undefined-name:L2 base" in capsys.readouterr().out
