"""`.env.example` must stay loadable by a fresh `cp .env.example && ava start`.

Two layers of the same invariant:

- the narrow regression: a present-but-empty env value (`KEY=` with nothing
  after it) sets an empty string. That is fine for a `str` field, but for an
  `int` / `bool` / `float` field it fails parsing even when the field has a
  default — the default only applies when the var is ABSENT, not
  present-but-empty — and aborts settings load. So the template must never
  ship a blank value for a typed field.
- the whole-class invariant: the template, loaded verbatim into the process
  environment exactly like `cp .env.example $AVA_HOME/.env` + the dotenv boot
  would, must construct `Settings()` without a ValidationError.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import AliasChoices

from shared.config import FIELD_INFOS, Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_example_values() -> dict[str, str]:
    """Parse `.env.example` with the same parser the runtime uses.

    `shared.dotenv_boot.load_ava_env` loads `$AVA_HOME/.env` via python-dotenv,
    so parsing the template with `dotenv_values` reproduces byte-for-byte what
    a fresh `cp .env.example $AVA_HOME/.env` puts into the process environment
    (`KEY=` becomes an empty string; commented lines are skipped). A key with
    no `=` at all parses to None — the template must not contain those, so we
    fail loudly instead of dropping the line.
    """
    values = dotenv_values(_REPO_ROOT / ".env.example")
    assert values, ".env.example parsed to nothing — file missing or parser broke"
    no_value_keys = [key for key, value in values.items() if value is None]
    assert not no_value_keys, f".env.example has keys without '=': {no_value_keys}"
    return {key: value for key, value in values.items() if value is not None}


def _settings_env_names() -> set[str]:
    """Every env var name `Settings` can read a field from.

    Collects, per field: the validation alias (a plain string or every string
    choice of an `AliasChoices`), the field alias, and the uppercased field
    name (the pydantic-settings fallback when no alias is declared). Used to
    scrub the test process environment so the template is the only source.
    """
    names: set[str] = set()
    for field_name, field in FIELD_INFOS.items():
        names.add(field_name.upper())
        if field.alias:
            names.add(field.alias)
        if field.serialization_alias:
            names.add(field.serialization_alias)
        validation_alias = field.validation_alias
        if isinstance(validation_alias, str):
            names.add(validation_alias)
        elif isinstance(validation_alias, AliasChoices):
            names.update(choice for choice in validation_alias.choices if isinstance(choice, str))
    return names


def test_env_example_has_no_blank_typed_fields() -> None:
    """No `.env.example` line may leave an int/bool/float field blank."""
    typed_aliases = {
        f.alias: f.annotation
        for f in FIELD_INFOS.values()
        if f.alias and f.annotation in (int, bool, float)
    }

    offenders = [
        key for key, value in _env_example_values().items() if value == "" and key in typed_aliases
    ]

    assert not offenders, (
        f".env.example ships blank values for typed fields {offenders}: an empty "
        "int/bool/float fails parsing and aborts settings load on a fresh "
        "`cp .env.example` (the field default applies only when the var is absent)."
    )


def test_env_example_loads_as_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The template, loaded verbatim, must construct `Settings()`.

    Simulates a fresh install: scrub every env var Settings recognizes (the
    test process carries real AVA_* values from conftest / the developer
    shell), inject exactly the template's key/value pairs — including the
    present-but-empty `KEY=` lines — and instantiate. Any ValidationError here
    is a template bug that would abort `ava start` right after
    `cp .env.example $AVA_HOME/.env`.
    """
    for name in sorted(_settings_env_names()):
        monkeypatch.delenv(name, raising=False)
    for key, value in _env_example_values().items():
        monkeypatch.setenv(key, value)
    Settings()
