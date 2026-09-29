"""Importable home for code shared by built-in skills' scripts.

A hyphenated skill directory under `ava_builtins/skills/` cannot itself be a
Python package, so a skill script that needs to share code with a sibling
script (or with another skill) puts that code here instead of reaching for it
by file path (`sys.path` edits, `importlib.util.spec_from_file_location`,
`runpy.run_path` — the pattern Rule 6 in `scripts/lint/lint_code_structure.py`
forbids under `ava_builtins/`). One subpackage per skill (or skill family);
the skill's own scripts stay thin entry points that import from here.
"""
