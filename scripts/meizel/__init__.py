"""Meizel: locality as a nested dependency DAG.

The checker derives fact edges (which file uses which) from first-party Python
source without importing it, and judges them against the `meizel.toml` manifests
that declare each directory's local graph. Run `python -m scripts.meizel --help`.
"""
