---
type: doc
title: "meizel/ — Python subprocesses"
description: "A Python interpreter started as a subprocess may run only a literal repository file or module, which is an edge; -c, temporary files and standard input are restricted forms."
tags:
  - locality
  - tool
---

# meizel/ — Python subprocesses

## What is it

A subprocess that starts Python runs code the checker would otherwise never see: a source
string passed to `-c`, a temporary file, a script read from standard input. Its imports,
patches and private names escape every rule. So an argument list whose first element starts
a Python interpreter may continue only in two ways, and anything else is a restricted form.

The first element is the interpreter when it is `sys.executable`, a literal path ending in
`python`, `python3` or `python3.x`, or a name bound once to one of those. A bare literal
`python` / `python3` counts only as the first argument of `subprocess.run/call/check_call/
check_output/Popen`, `base.host.proc.run_bounded`, `os.execv/execve/execvp/execvpe` and
`os.spawnv/spawnve/spawnvp/spawnvpe` (the list argument), and `asyncio.create_subprocess_exec`,
`os.execl/execle/execlp/execlpe` and `os.spawnl/spawnle/spawnlp/spawnlpe` (arguments unpacked),
so tables and descriptions of a process under test that happen to start with the same word are
not commands. A `sys.executable` list is a command wherever it appears. A shell string with
`python -c` is read by `subprocess.*(shell=True)`, `os.system`, `os.popen` and
`asyncio.create_subprocess_shell`.

After the interpreter, flags are skipped (`-I -B -u -E -s ...`; `-X` and `-W` take a value),
and the next element decides:

| what follows | result |
|---|---|
| `-m <module>`, a literal module name | for a first-party module, an edge from the file to the module's file (a package: its `__init__.py` and its `__main__.py`, which `-m` runs); a stdlib or third-party module makes none; a first-party name that names no tracked module is an `unresolved-import` error |
| a literal repository file | an edge from the file to that file; a path that names no tracked file is an `unresolved-import` error; a file that starts itself again (`__file__`) makes no edge |
| `-c` and whatever follows | `restricted-python`: `python -c`, whether the source is a literal, imports first-party code or not |
| `-` | `restricted-python`: source on standard input |
| anything else: a path built at run time (a temporary file, a parameter, an f-string), or arguments built with `*` | `restricted-python` |

A literal repository file is a string, `Path("...")`, `Path(__file__)` and `.resolve()`,
`.parent` (any number of times), `.parents[n]`, `.with_name("...")`, `/ "..."`, `str(...)`,
`<first-party module>.__file__`, or a name bound once to such an expression. No other
spelling is accepted: a path built another way is a path the checker cannot see.

The edge is judged like an import: it must be declared in the manifest, enter a directory
through its exports, avoid underscore modules (`__main__.py` and `__init__.py` are the language's own names, not
private ones) and close no cycle. A test that runs a child
script by path therefore depends on that script in the graph, and a script placed in the
tests of the component it imports is an ordinary forward edge.

A shell string with `python -c` (`subprocess.run("python -c '...'", shell=True)`) is still
read by what it imports: `restricted-python` only when the source imports first-party code.

Like every restricted form, these are debt that can be recorded in a baseline and only
shrink; `[loads]` and `[external]` exempt a file as before. To repay: move the source into a
file in the directory of the code it imports, run it by literal path or `-m`, and pass what
varies as arguments.

Parent: [[meizel/docs/checker/checker.meiva.okf.md|the checker]].
