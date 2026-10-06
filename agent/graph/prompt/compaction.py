"""Forced-compaction request text, independent of standing SDK exposure."""

from inspect import cleandoc


def compact_contract() -> str:
    """Disclose the SDK-owned contract when the summary model cannot call tools."""
    from ava.self import compact

    if compact.__doc__ is None:
        raise RuntimeError("compaction requires its SDK contract")
    return cleandoc(compact.__doc__)
