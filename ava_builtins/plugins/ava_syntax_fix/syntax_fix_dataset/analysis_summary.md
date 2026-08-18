# Syntax Fix v3 — Log Analysis Summary

## Data Source
- Log directory: ~/.ava/logs/
- Total log files scanned: 722
- Analysis date: 2026-06-29

## Key Metrics

| Metric | Count |
|--------|-------|
| compile_failed events | 52 |
| llm_repair events | 116 |
| syntax_fix applied | 702 |
| compile_failed rate | 0.15% (52/34,234) |

## Critical Finding

**96.2% (50/52) of compile_failed events had NO prior deterministic fix attempt.**
This means the existing pipeline (Chinese punctuation, invalid escape, missing
imports, ruff) does not catch these failure modes. The new v3 deterministic fixes
specifically target these uncaught patterns.

## compile_failed Error Categories

```
- other: 24
- string_newlines: 10
- backslash_trailing: 5
- general_syntax: 4
- fstring_expr: 4
- indentation: 3
```

## Most Common syntax_fix Interventions

- ruff: 266 (unused imports, formatting)
- missing_imports: 160+ (auto-adding imports)
- ruff_format: 80 (style normalization)
- invalid_escape: ~200 (escape sequence fixes)

## Repeated-Edit Pattern (Hidden Failure Mode)

The "invalid file -> repeated edit -> no SyntaxError" pattern occurs when:
1. Agent writes invalid Python to a .py file via ava.files.write()
2. write() performs NO syntax validation
3. Agent later tries to fix the file via ava.files.edit()
4. No SyntaxError is raised during the edit (compile not triggered)
5. Error only surfaces when the file is imported/executed later

**Recommendation**: Add compile() check to ava.files.write() for .py files,
reusing the deterministic fix pipeline (without LLM repair).
