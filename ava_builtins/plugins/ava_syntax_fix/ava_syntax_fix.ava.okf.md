---
type: doc
title: ava_syntax_fix — Syntax Fix Plugin
description: '`ava_syntax_fix` automatically fixes common Python syntax errors before code execution. It is a defensive fix (rather than letting the agent fix itself), because the round-trip cost is high—having the model regenerate vs silently fixing and then executing, the latter is faster and usually correct.'
tags:
- extensions
- plugin
- agent-extension
---

# ava_syntax_fix — Syntax Fix Plugin

## What it is

`ava_syntax_fix` automatically fixes common Python syntax errors before code execution. It is a defensive fix (rather than letting the agent fix itself), because the round-trip cost is high—having the model regenerate vs silently fixing and then executing, the latter is faster and usually correct.

## Registered hooks

### Pre-execution fix (before_exec hook)

```python
class _SyntaxFixHook(Hook):
    async def __call__(
        self, state: AgentState, _runtime: Runtime[AvaContext], config: RunnableConfig, /
    ) -> dict | None: ...

syntax_fix_before_exec = _SyntaxFixHook()
register_before_exec(syntax_fix_before_exec)
```

Code is taken from the last `AIMessage.tool_calls` (via `first_tool_call_code` extraction, not the hook parameter).
Multi-stage pipeline: the main pipeline in `plugin.py`, followed by a batch of deterministic fix batteries
(`_deterministic_fixes.py`), and finally compile validation + LLM fallback.

### 1. Chinese/fullwidth punctuation → ASCII (manual, not covered by ruff)

- `_translate_outside_strings` based on Python `tokenize` only replaces **outside strings**, preserving Chinese content inside strings
- `，`→`,`、`。`→`.`、`：`→`:`、fullwidth quotation marks `"" ''` → ASCII quotation marks
  (`_FULLWIDTH_QUOTE_MAP` first, `_PUNCT_MAP` after)
- Must run first, otherwise subsequent ruff/tokenize parsing fails

### 2. Missing imports → auto-add (ruff F821 + mapping table)

- ruff `F821` (undefined name, scope-aware) detects undefined names
- Manual mapping tables like `_BARE_NAME_IMPORTS`: `Path`, `datetime`, `json`, `np`→`numpy`,
  `pd`→`pandas`, **`ava`** (added to auto-import list), etc.
- Only fixes names known in the mapping table; unknown undefined names are left alone (for agent to handle)

### 3. Illegal escape sequence repair (manual, tokenize)

- Python 3.12 turned illegal escape sequences from warning to error; `_fix_invalid_escapes` does tokenize-level repair, e.g., `\s` → `\\s` (inside string literals)

### 4. `ruff check --fix`

- Handles unused imports, import sorting, trailing whitespace, blank lines, and other safe automatic fixes

### 5. `ruff format` (gated)

- Controlled by `settings.syntax_fix_ruff_format` switch; beyond the deterministically correct path, only does style normalization

### 6. Deterministic fix batteries (`_deterministic_fixes.py` / `apply_all_deterministic_fixes`)

After ruff, a batch of fixers targets failure patterns observed in production logs (unicode punctuation, string line breaks,
unclosed triple quotes, indentation, trailing backslash, missing comma, bracket pairing, empty f-string, unterminated string to triple quotes,
nested triple quotes, inner quote escaping). **Each fix is independently compile-guarded**: only adopted if it makes the source **compile**,
first compile-passing fix wins—prevents "legitimate code corrupted" or "greedy delimiter search silently merging source".

### 7. compile validation + LLM fallback

`compile()` pre-check; if still failing, calls `_llm_repair_syntax` to let the model fix it (only adopted if its result compiles),
if that still fails, injects the compile error as a ToolMessage, letting the agent repair itself.

## Key dependencies

- [[agent/hooks/hooks.ava.okf.md]] — before_exec hook
- [[tool-exec.ava.okf.md]] — code execution sandbox (where before_exec hook runs)
- `_deterministic_fixes.py` (26631 bytes) — compile-guarded deterministic fix batteries (step 6), fixer table strongly typed as `list[tuple[str, Callable[[str], tuple[str, int]]]]`

## Configuration

- ruff config in `pyproject.toml [tool.ruff]`
- `settings.syntax_fix_ruff_format`: whether to run step 5 `ruff format`
- Chinese punctuation maps: `_PUNCT_MAP` / `_FULLWIDTH_QUOTE_MAP`
- Import maps: `_BARE_NAME_IMPORTS` etc. (stdlib + third-party aliases + `ava`)

## Notes

- This is the only plugin that modifies agent code — other plugins only change messages or prompts
- Stage order is load-bearing: punctuation fixed first (otherwise ruff/tokenize parsing fails), then imports, escapes, ruff --fix,
  ruff format, deterministic batteries, finally compile + LLM fallback
- "Silent fix" strategy: the agent usually does not know its code was fixed (if it compiles, it executes directly)
- compile-guard is a key lesson: verify that a fixer not only leaves good input unchanged but also fixes bad input with correct semantics (AST not collapsed / arguments not lost)
