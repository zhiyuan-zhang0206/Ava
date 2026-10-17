---
type: doc
title: Native pytest fixture help
description: Static identity proof for pytest's original fixture docstring consumer.
tags: [scripts, lint, documentation, testing]
---

# Native pytest fixture help

Native pytest fixture help is a separate runtime field: pytest reads the original
fixture function's docstring for `--fixtures` and `--fixtures-per-test`. Static
recognition accepts a unique direct module fixture or a method in an undecorated,
unique top-level class, with one `pytest.fixture` decorator from a proved import.
Import aliases are supported; visible rebinding, star imports, mutations of the
fixture decorator and unknown decorator stacks are not proof. Dynamic factories,
nested registration and third-party fixture decorators are outside this boundary.
Declaration-time writes in class bodies and definition headers also invalidate
proof; ordinary class-local names do not shadow the module import.
Only the function docstring is help; body prose and helper docstrings remain strict.
The native consumer does not prove that each sentence is useful or accurate.

Parent: [[scripts/content_lint/code_comments/docs/code_comments.meiva.okf.md]].
