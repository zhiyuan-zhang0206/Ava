"""Meizel: the locality rules and the checker that judges them.

Locality is a nested dependency DAG: the checker derives fact edges (which file
uses which) from first-party Python source without importing it, and judges them
against the `meizel.toml` manifests that declare each directory's local graph.
Run `python -m meizel --help`. The rule groups (`budgets`, `owners`, `state`,
`language`) are driven by `scripts/lint/code_structure.py` until the checker
replaces that gate.
"""
