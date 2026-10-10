"""Resource prerequisites follow fixture dependencies rather than test paths."""

from pathlib import Path

import pytest


def test_direct_unit_does_not_request_e2e_process_environment(
    request: pytest.FixtureRequest,
) -> None:
    assert "_e2e_process_env" not in request.fixturenames
    assert "frontend_proc" not in request.fixturenames
    assert "playwright_runtime" not in request.fixturenames


def test_missing_frontend_skips_only_its_fixture_consumers(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A separate pytest process owns Playwright's soft-assertion scope and bootstrap.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    pytester.makeconftest(
        """
        pytest_plugins = ["tests.fixtures.env_bootstrap", "tests.fixtures.leak_guard"]

        def pytest_collection_modifyitems(session, config, items):
            from tests.e2e import conftest as e2e_fixtures

            e2e_fixtures.shutil.which = lambda command: None
            e2e_fixtures.sweep_stale_e2e_processes = lambda: None
            e2e_fixtures.pytest_collection_modifyitems(session, config, items)
        """
    )
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def frontend_proc():
            raise AssertionError("missing frontend must skip before setup")

        @pytest.fixture
        def indirect_frontend(frontend_proc):
            return frontend_proc

        @pytest.fixture
        def playwright_runtime():
            raise AssertionError("missing frontend must skip before browser setup")

        def test_direct():
            assert True

        def test_frontend(indirect_frontend):
            assert False

        def test_browser(playwright_runtime):
            assert False
        """
    )
    result = pytester.runpytest_subprocess("-n", "0", "-q")
    result.assert_outcomes(passed=1, skipped=2)
