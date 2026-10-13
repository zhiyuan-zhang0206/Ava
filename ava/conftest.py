"""Make pytest import the `ava` package before any conftest below it.

`--import-mode=importlib` builds a conftest's module name from its path and imports the
missing parent packages itself. When `ava` is the first thing imported, pytest imports it
as the parent of `ava.sdk_surface`, and `ava/__init__.py` imports `ava.sdk_surface` on its
own; pytest then executes `ava/sdk_surface/__init__.py` a second time and installs that
second package object over the first. Its submodules stay in `sys.modules` but are no
longer attributes of the package, so `ava.sdk_surface.agent_identity` raises
`AttributeError` in any test run that loads such a conftest (the order of the arguments
decides, which is why the failure looked random). A conftest here makes pytest import
`ava` first, as an ordinary parent, so the packages below it are imported once.
"""
