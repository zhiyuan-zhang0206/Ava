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
Only the function docstring is help; body prose and helper docstrings remain strict.
The native consumer does not prove that each sentence is useful or accurate.

```ava-contract
id = "native-fixture-help"
kind = "external"
statement = "Pytest fixture docstrings remain native CLI help only when the parser proves the original pytest.fixture binding and the fixture definition is unique. This does not authorize ordinary function docstrings, fixture-body comments, replaced declarations or unknown decorators. Pytest reads the fixture function with inspect.getdoc for both fixture help commands."
code = ["scripts/content_lint/code_comments/fixtures.py:fixture_owners"]
tests = ["scripts/content_lint/code_comments/tests/test_fixtures.py::test_native_fixture_help_is_not_internal_prose", "scripts/content_lint/code_comments/tests/test_fixtures.py::test_fixture_help_requires_unambiguous_import_identity", "scripts/content_lint/code_comments/tests/test_fixtures.py::test_fixture_identity_does_not_authorize_body_prose", "scripts/content_lint/code_comments/tests/test_fixtures.py::test_replaced_fixture_binding_does_not_authorize_help", "scripts/content_lint/code_comments/tests/test_fixtures.py::test_unproved_class_identity_does_not_authorize_fixture_help"]
source = "https://docs.pytest.org/en/9.1.x/how-to/fixtures.html"
applies_to = "pytest 9.1.1, _pytest.fixtures native CLI help consumer"
```

Parent: [[scripts/content_lint/code_comments/docs/code_comments.ava.okf.md]].
