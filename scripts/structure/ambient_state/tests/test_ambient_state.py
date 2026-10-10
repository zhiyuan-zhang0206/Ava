"""Unit coverage for the ambient-state rule's state shapes (scripts/structure/ambient_state/):
containers, instances, memoized functions, global and foreign rebinding, class-level state — what
each reports and the known ways around it. Sources are measured directly against a synthetic
repository root (no lcs.main, no git — see scripts/lint/tests/test_ambient_state_gate.py for the
gate wiring and test_ambient_effects.py for imports, background work, scope and the closed lists)."""

from __future__ import annotations

import ast
import pathlib
import textwrap

import pytest

from scripts.structure import ambient_state
from scripts.structure.ambient_state import allowlist as allow


def _write(root: pathlib.Path, name: str, content: str) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _sites(
    root: pathlib.Path,
    source: str,
    rel: str = "base/mod.py",
    files: dict[str, str] | None = None,
) -> dict[str, int]:
    """`rule:name -> site count` for one module; `files` are other repo files it can see."""
    for name, content in (files or {}).items():
        _write(root, name, content)
    path = _write(root, rel, source)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    measured = ambient_state.measure(tree, rel, root)
    return {key.split("::", 1)[1]: len(lines) for key, lines in measured.items()}


# --- containers ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["{}", "[]", "set()", "dict()", "defaultdict(list)", "deque(maxlen=8)", "WeakKeyDictionary()"],
)
def test_an_empty_container_is_ambient_state(tmp_path: pathlib.Path, value: str) -> None:
    sites = _sites(tmp_path, f"_REGISTRY = {value}\n")

    assert sites == {"ambient-container:_REGISTRY": 1}


def test_an_annotated_empty_container_is_ambient_state(tmp_path: pathlib.Path) -> None:
    assert _sites(tmp_path, "_CACHE: dict[str, int] = {}\n") == {"ambient-container:_CACHE": 1}


def test_a_single_element_flag_list_is_ambient_only_when_the_module_flips_it(
    tmp_path: pathlib.Path,
) -> None:
    """`_BUILT = [False]` is the classic way around a bare global: a mutable cell."""
    flipped = """
        _BUILT = [False]

        def build():
            _BUILT[0] = True
    """
    assert _sites(tmp_path, flipped) == {"ambient-container:_BUILT": 1}
    assert _sites(tmp_path, "_BUILT = [False]\n") == {}


@pytest.mark.parametrize(
    "mutation",
    [
        "_TABLE.update(extra)",
        "_TABLE['k'] = v",
        "del _TABLE['k']",
        "_TABLE['k'] += 1",
        "_TABLE['k'].append(v)",
    ],
)
def test_a_filled_table_is_ambient_when_the_module_mutates_it(
    tmp_path: pathlib.Path, mutation: str
) -> None:
    source = f"_TABLE = {{'a': [1]}}\n\ndef fill(extra, v):\n    {mutation}\n"

    assert _sites(tmp_path, source) == {"ambient-container:_TABLE": 1}


def test_a_registry_filled_at_import_is_ambient(tmp_path: pathlib.Path) -> None:
    source = """
        _HANDLERS = {"a": 1}
        _HANDLERS["b"] = 2
        _ORDER = ["a"]
        _ORDER += ["b"]
    """
    assert _sites(tmp_path, source) == {
        "ambient-container:_HANDLERS": 1,
        "ambient-container:_ORDER": 1,
    }


def test_a_constant_table_nobody_mutates_is_not_ambient(tmp_path: pathlib.Path) -> None:
    source = """
        _NAMES = ["a", "b"]
        _TABLE = {"a": 1}
        _SQUARES = {n: n * n for n in range(4)}
    """
    assert _sites(tmp_path, source) == {}


def test_a_local_of_the_same_name_is_not_a_mutation_of_the_module_table(
    tmp_path: pathlib.Path,
) -> None:
    source = """
        _TABLE = {"a": 1}

        def f(_TABLE):
            _TABLE["b"] = 2

        def g():
            _TABLE = {}
            _TABLE.update({"c": 3})
    """
    assert _sites(tmp_path, source) == {}


def test_a_global_declared_name_is_still_the_module_table(tmp_path: pathlib.Path) -> None:
    source = """
        _TABLE = {"a": 1}

        def f():
            global _TABLE
            _TABLE.update({"b": 2})
    """
    assert _sites(tmp_path, source) == {"ambient-container:_TABLE": 1}


# --- instances, holders, wiring, constants ---------------------------------------------------


def test_a_holder_object_is_an_ambient_instance(tmp_path: pathlib.Path) -> None:
    """`_State()` carrying mutable attributes is a module global in disguise."""
    source = """
        class _State:
            def __init__(self):
                self.ready = False

        _STATE = _State()
    """
    assert _sites(tmp_path, source) == {"ambient-instance:_STATE": 1}


def test_a_module_level_instance_of_an_unknown_callee_is_ambient(tmp_path: pathlib.Path) -> None:
    source = """
        from base.clients import make_client

        _CLIENT = make_client()
    """
    assert _sites(tmp_path, source) == {"ambient-instance:_CLIENT": 1}


def test_a_namespace_holder_is_an_ambient_instance(tmp_path: pathlib.Path) -> None:
    source = """
        from types import SimpleNamespace

        _BAG = SimpleNamespace(ready=False)
    """
    assert _sites(tmp_path, source) == {"ambient-instance:_BAG": 1}


@pytest.mark.parametrize(
    "definition",
    [
        "class _Marker:\n    def ping(self):\n        return 1\n\n_MARK = _Marker()",
        (
            "from dataclasses import dataclass\n\n@dataclass(frozen=True)\n"
            "class _Limits:\n    cap: int\n\n_LIMITS = _Limits(3)"
        ),
        (
            "from typing import NamedTuple\n\nclass _Pair(NamedTuple):\n    a: int\n\n"
            "_PAIR = _Pair(1)"
        ),
        "from enum import Enum\n\nclass _Mode(Enum):\n    A = 1\n\n_MODE = _Mode(1)",
    ],
    ids=["stateless-class", "frozen-dataclass", "namedtuple", "enum"],
)
def test_an_instance_of_a_value_class_is_not_ambient(
    tmp_path: pathlib.Path, definition: str
) -> None:
    assert _sites(tmp_path, definition + "\n") == {}


def test_a_value_class_defined_in_another_module_is_resolved_through_the_import(
    tmp_path: pathlib.Path,
) -> None:
    policy = """
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Policy:
            retries: int
    """
    mutable = """
        class Tally:
            def __init__(self):
                self.n = 0
    """
    reexport = "from base.policy import Policy\n"
    ok = "from base.policy import Policy\n\nDEFAULT = Policy(3)\n"
    via_package = "from base import Policy\n\nDEFAULT = Policy(3)\n"
    bad = "from base.tally import Tally\n\nTOTAL = Tally()\n"

    assert _sites(tmp_path, ok, files={"base/policy.py": policy}) == {}
    package = {"base/__init__.py": reexport, "base/policy.py": policy}
    assert _sites(tmp_path, via_package, rel="gateway/mod.py", files=package) == {}
    assert _sites(tmp_path, bad, files={"base/tally.py": mutable}) == {"ambient-instance:TOTAL": 1}


@pytest.mark.parametrize(
    "import_line", ["from base.policy import Policy as P", "from base import Policy as P"]
)
def test_a_direct_classmethod_value_factory_resolves_its_definition(
    tmp_path: pathlib.Path, import_line: str
) -> None:
    policy = '''
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Policy:
            limit: int

            @classmethod
            def snapshot(bound, limit) -> object:
                """The annotation is irrelevant to the concrete constructor."""
                return bound(limit=limit)
    '''
    files = {"base/policy.py": policy, "base/__init__.py": "from base.policy import Policy\n"}
    assert _sites(tmp_path, import_line + "\nVALUE = P.snapshot(3)\n", files=files) == {}


@pytest.mark.parametrize(
    ("decorators", "body"),
    [
        ("@classmethod", "return []"),
        ("@classmethod", "return {}"),
        ("@classmethod", "return set()"),
        ("@classmethod", "return make_value()"),
        ("@classmethod", "return cls.other()"),
        ("@classmethod", "return cls(3) if flag else []"),
        ("@classmethod", "cls = list\nreturn cls()"),
        ("@classmethod", "def nested():\n    return cls(3)\nreturn nested()"),
        ("@staticmethod", "return cls(3)"),
        ("", "return cls(3)"),
        ("@unknown\n@classmethod", "return cls(3)"),
    ],
)
def test_factory_annotations_and_unknown_returns_do_not_prove_a_value(
    tmp_path: pathlib.Path, decorators: str, body: str
) -> None:
    policy = (
        "from dataclasses import dataclass\n@dataclass(frozen=True)\n"
        "class Policy:\n    limit: int\n"
        + textwrap.indent(decorators + "\n" if decorators else "", "    ")
        + "    def capture(cls) -> Policy:\n"
        + textwrap.indent(body, "        ")
        + "\n"
    )
    assert _sites(
        tmp_path,
        "from base.policy import Policy\nVALUE = Policy.capture()\n",
        files={"base/policy.py": policy},
    ) == {"ambient-instance:VALUE": 1}


@pytest.mark.parametrize(
    "extra",
    [
        "capture = make_value",
        "classmethod = arbitrary_decorator",
        "from external import classmethod",
        "from external import capture",
        "del capture",
        "def classmethod(value):\n    return value",
    ],
)
def test_a_rebound_factory_or_decorator_remains_unknown(tmp_path: pathlib.Path, extra: str) -> None:
    policy = (
        "from dataclasses import dataclass\n@dataclass(frozen=True)\nclass Policy:\n"
        "    limit: int\n    @classmethod\n    def capture(cls):\n        return cls(3)\n"
        + textwrap.indent(extra, "    ")
        + "\n"
    )
    assert _sites(
        tmp_path,
        "from base.policy import Policy\nVALUE = Policy.capture()\n",
        files={"base/policy.py": policy},
    ) == {"ambient-instance:VALUE": 1}


def test_a_classmethod_constructor_does_not_make_a_mutable_class_a_value(
    tmp_path: pathlib.Path,
) -> None:
    policy = """
        from dataclasses import dataclass
        @dataclass
        class Policy:
            limit: int
            @classmethod
            def capture(cls):
                return cls(3)
    """
    assert _sites(
        tmp_path,
        "from base.policy import Policy\nVALUE = Policy.capture()\n",
        files={"base/policy.py": policy},
    ) == {"ambient-instance:VALUE": 1}


@pytest.mark.parametrize(
    "line",
    [
        "_RX = re.compile('a+')",
        "_T = TypeVar('_T')",
        "_DAY = timedelta(days=1)",
        "_ROOT = Path('/etc')",
        "_ADAPTER = TypeAdapter(int)",
        "_NAMES = frozenset({'a'})",
        "_MARK = object()",
        "_DIGEST = hashlib.sha256(b'').hexdigest()",
        "_LOCK = threading.Lock()",
        "_RLOCK = threading.RLock()",
        "_READY = threading.Event()",
        "_GATE = asyncio.Semaphore(2)",
        "_LOCAL = threading.local()",
        "app = FastAPI()",
        "router = APIRouter()",
        "_LOG = logging.getLogger('x')",
        "_COUNTER = meter.create_counter('x')",
    ],
)
def test_wiring_pure_constants_and_write_only_handles_are_not_ambient(
    tmp_path: pathlib.Path, line: str
) -> None:
    header = (
        "import asyncio, hashlib, logging, re, threading\n"
        "from datetime import timedelta\nfrom pathlib import Path\n"
        "from typing import TypeVar\nfrom fastapi import APIRouter, FastAPI\n"
        "from pydantic import TypeAdapter\n\n"
    )
    assert _sites(tmp_path, header + line + "\n") == {}


def test_a_bare_hasher_is_a_stateful_instance(tmp_path: pathlib.Path) -> None:
    source = "import hashlib\n\n_HASH = hashlib.sha256()\n"

    assert _sites(tmp_path, source) == {"ambient-instance:_HASH": 1}


@pytest.mark.parametrize(
    "line",
    [
        "_V = ContextVar('v')",
        "_V = ContextVar[int]('v', default=0)",
        "_V = contextvars.ContextVar('v')",
        "_V = CV('v')",
    ],
)
def test_a_contextvar_is_its_own_rule_not_wiring(tmp_path: pathlib.Path, line: str) -> None:
    header = "import contextvars\nfrom contextvars import ContextVar\nfrom contextvars import ContextVar as CV\n\n"

    assert _sites(tmp_path, header + line + "\n") == {"contextvar:_V": 1}


# --- memoization -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decorator", "args", "rule"),
    [
        ("@functools.lru_cache(maxsize=1)", "", "hidden-singleton"),
        ("@lru_cache", "", "hidden-singleton"),
        ("@functools.cache", "", "hidden-singleton"),
        ("@functools.cache", "key", "hidden-cache"),
        ("@lru_cache(maxsize=8)", "a, b", "hidden-cache"),
    ],
)
def test_a_memoized_module_function_is_a_hidden_singleton_or_cache(
    tmp_path: pathlib.Path, decorator: str, args: str, rule: str
) -> None:
    source = f"import functools\nfrom functools import lru_cache\n\n{decorator}\ndef load({args}):\n    return 1\n"

    assert _sites(tmp_path, source) == {f"{rule}:load": 1}


def test_a_cached_property_is_per_instance_and_not_reported(tmp_path: pathlib.Path) -> None:
    source = """
        from functools import cached_property

        class Box:
            @cached_property
            def value(self):
                return 1
    """
    assert _sites(tmp_path, source) == {}


def test_a_memoized_pure_function_goes_through_the_closed_list(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "from functools import cache\n\n@cache\ndef table():\n    return {}\n"
    assert _sites(tmp_path, source) == {"hidden-singleton:table": 1}

    monkeypatch.setattr(allow, "ALLOWED", {"base/mod.py::hidden-singleton:table": "a static table"})
    assert _sites(tmp_path, source) == {}


# --- global rebinding ----------------------------------------------------------------------------


def test_a_function_that_rebinds_a_global_is_reported_once_per_function(
    tmp_path: pathlib.Path,
) -> None:
    source = """
        _CLIENT = None

        def open_client():
            global _CLIENT
            _CLIENT = object()

        def close_client():
            global _CLIENT
            _CLIENT = None

        def use():
            global _CLIENT
            return _CLIENT
    """
    assert _sites(tmp_path, source) == {"global-rebind:_CLIENT": 2}


@pytest.mark.parametrize(
    "body",
    ["_N += 1", "import json as _N", "del _N", "for _N in range(3):\n        pass"],
)
def test_every_way_of_rebinding_a_declared_global_counts(tmp_path: pathlib.Path, body: str) -> None:
    source = f"_N = 0\n\ndef bump():\n    global _N\n    {body}\n"

    assert _sites(tmp_path, source) == {"global-rebind:_N": 1}


def test_assigning_through_globals_is_a_rebind(tmp_path: pathlib.Path) -> None:
    source = "def install(value):\n    globals()['_SLOT'] = value\n"

    assert _sites(tmp_path, source) == {"global-rebind:globals()": 1}


def test_a_local_of_the_same_name_without_a_global_declaration_is_not_a_rebind(
    tmp_path: pathlib.Path,
) -> None:
    source = "_N = 0\n\ndef f():\n    _N = 1\n    return _N\n"

    assert _sites(tmp_path, source) == {}


# --- rebinding another module's attribute --------------------------------------------------------


def test_assigning_an_attribute_of_another_first_party_module_is_foreign(
    tmp_path: pathlib.Path,
) -> None:
    provider = "CURRENT = None\n"
    source = """
        import base.provider as provider

        def enter(name):
            provider.CURRENT = name
    """
    assert _sites(tmp_path, source, files={"base/provider.py": provider}) == {
        "foreign-rebind:base.provider.CURRENT": 1
    }


def test_a_function_level_import_does_not_hide_a_foreign_rebind(tmp_path: pathlib.Path) -> None:
    source = """
        def enter(name):
            from base import provider

            provider.CURRENT = name
    """
    assert _sites(tmp_path, source, files={"base/provider.py": "CURRENT = None\n"}) == {
        "foreign-rebind:base.provider.CURRENT": 1
    }


def test_setattr_on_a_first_party_module_is_foreign(tmp_path: pathlib.Path) -> None:
    source = """
        import base.provider as provider

        def enter(name):
            setattr(provider, "CURRENT", name)
    """
    assert _sites(tmp_path, source, files={"base/provider.py": "CURRENT = None\n"}) == {
        "foreign-rebind:setattr": 1
    }


def test_assigning_to_third_party_modules_objects_or_self_is_not_foreign(
    tmp_path: pathlib.Path,
) -> None:
    source = """
        import sys

        class Box:
            def __init__(self):
                self.value = 1

        def tweak(thing):
            sys.stdout = None
            thing.value = 2
    """
    assert _sites(tmp_path, source) == {}


# --- class-level state ----------------------------------------------------------------------------


def test_a_mutable_class_attribute_is_shared_state(tmp_path: pathlib.Path) -> None:
    source = """
        class Registry:
            _items = {}
            _seen: set[str] = set()
    """
    assert _sites(tmp_path, source) == {
        "class-level-container:Registry._items": 1,
        "class-level-container:Registry._seen": 1,
    }


def test_a_class_table_is_shared_state_only_when_a_method_mutates_it(
    tmp_path: pathlib.Path,
) -> None:
    mutated = """
        class Plugins:
            _loaded = ["core"]

            @classmethod
            def add(cls, name):
                cls._loaded.append(name)
    """
    assert _sites(tmp_path, mutated) == {"class-level-container:Plugins._loaded": 1}
    assert _sites(tmp_path, "class Plugins:\n    _loaded = ['core']\n") == {}


def test_declared_fields_are_not_shared_state_but_a_classvar_is(tmp_path: pathlib.Path) -> None:
    source = """
        from dataclasses import dataclass, field
        from enum import Enum
        from typing import ClassVar

        @dataclass
        class Config:
            tags: list[str] = field(default_factory=list)
            registry: ClassVar[dict[str, int]] = {}

        class Mode(Enum):
            A = {}
    """
    assert _sites(tmp_path, source) == {"class-level-container:Config.registry": 1}
