"""The AVA_OBSERVABILITY_URL base every station consumer validates the same way."""

from __future__ import annotations

import pytest

from base.telemetry.station_endpoint import validated_observability_base


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("   ", ""),
        ("http://10.0.0.46", "http://10.0.0.46"),
        ("https://station.example/", "https://station.example"),
        (" http://10.0.0.46/ ", "http://10.0.0.46"),
    ],
)
def test_well_formed_or_unset_value_is_returned_without_a_warning(
    raw: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert validated_observability_base(raw) == expected
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("ftp://10.0.0.46", "scheme must be http/https (got 'ftp')"),
        ("10.0.0.46:4318", "scheme must be http/https (got '')"),
        ("http://", "missing host"),
        ("http://10.0.0.46:3100", "port must be omitted"),
        ("http://10.0.0.46/otlp", "path must be omitted (got '/otlp')"),
    ],
)
def test_malformed_value_falls_back_to_empty_and_names_the_problem_on_stderr(
    raw: str, problem: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert validated_observability_base(raw) == ""
    err = capsys.readouterr().err
    assert "AVA_OBSERVABILITY_URL" in err
    assert problem in err
    assert "falling back to local loopback endpoints" in err
