"""`ava` settings-load failure surfaces a friendly env template, not a traceback.

A fresh host with no ~/.ava/.env (or one missing required fields) makes
`Settings()` raise pydantic ValidationError on first `cli.commands` import.
main() catches it and calls `_print_settings_load_failure`, which must turn the
raw error into a copy-paste list of the missing AVA_* vars plus the enroll hint.
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from cli.main import _print_settings_load_failure


def _validation_error(*fields: str) -> ValidationError:
    """Build a ValidationError whose missing locs are `fields` (Python attr names)."""
    ns = {"__annotations__": dict.fromkeys(fields, str)}
    model = type("_RequiredFields", (BaseModel,), ns)
    try:
        model()
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError for missing required fields")


def test_friendly_template_lists_missing_vars(capsys):
    rc = _print_settings_load_failure(_validation_error("db_url", "redis_url"))

    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    # Python attr names get uppercased + AVA_ prefixed to match the field aliases.
    assert "AVA_DB_URL=<value>" in err
    assert "AVA_REDIS_URL=<value>" in err
    # The enroll path is the recommended fix, not a raw traceback.
    assert "ava enroll" in err
    assert "Traceback" not in err


def test_already_prefixed_alias_not_double_prefixed(capsys):
    # A loc already shaped like an alias must not become AVA_AVA_X.
    _print_settings_load_failure(_validation_error("AVA_DB_URL"))

    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "AVA_DB_URL=<value>" in err
    assert "AVA_AVA_" not in err
