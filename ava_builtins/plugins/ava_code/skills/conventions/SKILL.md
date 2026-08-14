---
name: conventions
description: Depth on AGENTS.md auto-injection + the shell-vs-files tool choice — read when AGENTS.md isn't surfacing or you're unsure which tool to use.
---

# Ava Code — conventions & context files

The basics — set cwd, read `AGENTS.md` first, shell-vs-files tool choice — are
always present in your coding-tools section. This skill is the depth behind the
context-file injection and the tool-choice rule.

## AGENTS.md auto-injection

**Primary path** — after `ava.cwd.set`, `ava.files.read("AGENTS.md")` returns the project conventions; read and follow them.

**Fallback** — in case you forget, any `ava.files.read(other_file)` also surfaces `AGENTS.md`: the path from that file up to **the farther of git root or `$HOME`** is scanned, and any `AGENTS.md` along it is printed to stdout (content visible next turn).

Both paths share one dedup state (`injected_paths`): a path read by the primary path won't be re-printed by the fallback, and vice versa. After compact strips message history, the state auto-resets and re-surfaces.

`ava.shell.run("cat foo")` — reading via shell, bypassing the SDK — **does not** trigger injection. So read source via `ava.files.read`, not shell cat.

## shell vs files — examples

The rule of thumb: git/grep/tests/builds → `ava.shell.run`; known single-file reads/writes → `ava.files`. The easy-to-miss cases:

- ❌ `ava.shell.run("cat path/to/file.py")` → ✅ `ava.files.read("path/to/file.py")`
- ❌ `ava.files.read("*.py")` — files does not support wildcards → ✅ `ava.shell.run("ls *.py")`

Reading source via `ava.files.read` also triggers AGENTS.md auto-injection (above).

## grep vs rg — prefer ripgrep

`grep -rn` on this repo scans every `.worktrees/` checkout (each a full
tree — often dozens), so it often hits the 30 s timeout. `rg` (ripgrep) respects
`.gitignore` and is ~50x faster even without that advantage.

Benchmark on this repo:
- `rg 'pattern' --type py` → 0.07 s
- `grep -rn 'pattern' --include='*.py'` → >15 s (times out)
- `grep -rn 'pattern' --include='*.py' --exclude-dir=.worktrees` → 3.35 s

- ✅ `ava.shell.run("rg -n 'pattern'")` — fast, .gitignore-aware
- ✅ `ava.shell.run("rg -n 'pattern' --type py")` — limit to Python files
- ❌ `ava.shell.run("grep -rn 'pattern' . --include='*.py' | grep -v ...")` — slow, times out

`rg` is present on a standard Ava machine. If on a machine without it, fall back to
`grep -rn --exclude-dir=.worktrees --exclude-dir=.venv --exclude-dir=node_modules`.
