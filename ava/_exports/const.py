"""`ava.const()` — opt-in documented constant values.

The wrapper factory plus its per-type cache, split out of `ava/__init__.py`.
The package entry imports `const` BEFORE its submodule imports, so top-level
const assignments (e.g. `ava.self.AGENT_ID = ava.const(...)`) keep working
during submodule load.
"""

from typing import Any

_DOCUMENTED_TYPE_CACHE: dict[type, type] = {}


def const(value: Any, doc: str) -> Any:
    """Wrap a constant value with a docstring so `help(value)` shows the doc.

    Returns a `type(value)` subclass instance carrying `__doc__` set to `doc`.
    Use this for static module-level constants where the agent might pass the
    value directly to `help()` and expect documentation rather than just the
    runtime repr.

    The wrapper preserves the value's behavior: arithmetic, comparison,
    `isinstance` checks, and stringification all work as if it were the bare
    value. Only constants whose type is subclassable benefit — built-ins like
    `int`, `str`, `tuple`, `frozenset`, `Path`, `float` all work; `bool` and
    `NoneType` cannot be subclassed and will raise `TypeError`.

    For constants that mutate at runtime (e.g. `ava.self.AGENT_ID`), prefer PEP 224
    attribute docstrings instead — `help(ava)` lists them via AST scan and
    docs survive in-place reassignment.

    Args:
        value: the constant value.
        doc: short natural-language description, surfaced as the H1 body when
            `help(wrapped)` is called.

    Returns:
        A subclass instance of `type(value)` with `__doc__ == doc`. The
        instance is `==`, `is`-comparable (within Python's interning rules),
        and `isinstance(x, type(value))` returns `True`.

    Raises:
        TypeError: `type(value)` is not subclassable.
    """
    base = type(value)
    cls = _DOCUMENTED_TYPE_CACHE.get(base)
    if cls is None:
        cls = type(
            f"Documented{base.__name__}",
            (base,),
            {"__module__": __name__, "__qualname__": f"const.Documented{base.__name__}"},
        )
        _DOCUMENTED_TYPE_CACHE[base] = cls
    instance = cls(value)
    instance.__doc__ = doc
    return instance
