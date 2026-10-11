"""Child process for the `AVA_SDK_DISABLE` tests: import `ava`, install the SDK surface, run a scenario.

The disable machinery runs as the first step of the SDK install (`ava.sdk_surface.install`), not
at `import ava`: a real agent child reaches the load itself, so a bare script loads explicitly.
Usage: `child_sdk_disable.py <scenario>`; the parent sets `AVA_SDK_DISABLE`.
"""

import contextlib
import io
import sys
from collections.abc import Callable

import ava

ava.ensure_plugins_loaded()


def full_surface() -> None:
    print(",".join(sorted(ava.__all_for_ava__)))


def module_hides_from_parent() -> None:
    assert not hasattr(ava, "watcher"), "watcher should be gone from ava package"
    assert "watcher" not in ava.__all_for_ava__
    # Import succeeds and returns the disabled sentinel.
    import ava.watcher as w

    try:
        _ = w.anything  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    except AttributeError as e:
        msg = str(e)
        assert "disabled by AVA_SDK_DISABLE" in msg, msg
        assert "'watcher'" in msg, msg
        print("ok")
    else:
        raise AssertionError("attribute access on disabled module did not raise")


def attribute_keeps_module() -> None:
    assert hasattr(ava, "self"), "ava.self module should remain"
    assert hasattr(ava.self, "restart"), "restart should remain"
    assert not hasattr(ava.self, "terminate"), "terminate should be gone"
    try:
        _ = ava.self.terminate
    except AttributeError:
        print("ok")


def nested_module_sentinel() -> None:
    assert hasattr(ava, "shell"), "ava.shell should remain"
    assert hasattr(ava.shell, "run"), "ava.shell.run should remain"
    assert not hasattr(ava.shell, "sessions"), "ava.shell.sessions should be gone"
    import ava.shell.sessions as sess

    try:
        _ = sess.new
    except AttributeError as e:
        msg = str(e)
        assert "disabled by AVA_SDK_DISABLE" in msg, msg
        assert "shell.sessions" in msg, msg
        print("ok")
    else:
        raise AssertionError("attribute access on disabled nested module did not raise")


def multiple_disables() -> None:
    assert not hasattr(ava, "watcher")
    assert not hasattr(ava, "agents")
    assert not hasattr(ava.self, "terminate")
    assert hasattr(ava, "shell"), "shell not in disable list, should remain"
    print("ok")


def help_overview() -> None:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ava.help()
    output = buf.getvalue()
    assert "from . import agents" not in output, output
    assert "from . import files" in output, output
    print("ok")


def watcher_and_agents_gone() -> None:
    assert not hasattr(ava, "watcher")
    assert not hasattr(ava, "agents")
    print("ok")


def apply_is_idempotent() -> None:
    from ava.sdk_surface.sdk_disable import apply_sdk_disable

    # The install applied the env entries (the preamble loaded the surface).
    assert not hasattr(ava, "watcher")
    # A second call with the same entry is a no-op: no crash, no duplicate.
    apply_sdk_disable(["watcher"])
    assert not hasattr(ava, "watcher")
    print("ok")


def apply_is_cumulative() -> None:
    from ava.sdk_surface.sdk_disable import apply_sdk_disable

    # The env baseline disabled watcher at install time.
    assert not hasattr(ava, "watcher")
    assert hasattr(ava, "agents"), "agents still present before second call"
    apply_sdk_disable(["agents"])
    assert not hasattr(ava, "agents"), "agents should be gone after second call"
    assert not hasattr(ava, "watcher"), "watcher should remain gone"
    assert hasattr(ava, "files"), "files should remain"
    print("ok")


def apply_dotted_cumulative() -> None:
    from ava.sdk_surface.sdk_disable import apply_sdk_disable

    # The env baseline disabled self.terminate at install time.
    assert not hasattr(ava.self, "terminate")
    assert hasattr(ava.self, "restart"), "restart should remain"
    apply_sdk_disable(["self.restart"])
    assert not hasattr(ava.self, "terminate"), "terminate still gone"
    assert not hasattr(ava.self, "restart"), "restart now gone too"
    print("ok")


def applied_entries_tracked() -> None:
    from ava.sdk_surface import install as sdk_install
    from ava.sdk_surface.sdk_disable import apply_sdk_disable

    def disabled() -> object:
        installation = sdk_install.installed()
        assert installation is not None
        return installation.disabled

    # After the install: env entries are recorded on the installation.
    assert "watcher" in disabled()  # pyright: ignore[reportOperatorIssue]
    apply_sdk_disable(["agents"])
    assert "agents" in disabled()  # pyright: ignore[reportOperatorIssue]
    assert "watcher" in disabled()  # pyright: ignore[reportOperatorIssue]
    print("ok")


def help_with_skills_disabled() -> None:
    assert not hasattr(ava, "skills"), "skills should be gone from ava package"
    ava.help(ava.files)
    ava.help(ava)
    print("ok")


def noop() -> None:
    """Nothing beyond the install itself: the parent asserts how the install behaved."""


def runtime_refuses_framework_module() -> None:
    from ava.sdk_surface.sdk_disable import apply_sdk_disable

    for entry in ("sdk_surface", "sdk_surface.agent_identity.agent_id"):
        try:
            apply_sdk_disable([entry])
        except ValueError as exc:
            print("refused", entry, "framework module ava.sdk_surface" in str(exc))


def unknown_and_already_disabled_apply() -> None:
    from ava.sdk_surface import install as sdk_install
    from ava.sdk_surface.sdk_disable import apply_sdk_disable

    apply_sdk_disable(["not_a_real_namespace"])
    apply_sdk_disable(["agents"])
    apply_sdk_disable(["agents.spawn"])
    installation = sdk_install.installed()
    assert installation is not None
    print(sorted(installation.disabled))


SCENARIOS: dict[str, Callable[[], None]] = {
    "full_surface": full_surface,
    "module_hides_from_parent": module_hides_from_parent,
    "attribute_keeps_module": attribute_keeps_module,
    "nested_module_sentinel": nested_module_sentinel,
    "multiple_disables": multiple_disables,
    "help_overview": help_overview,
    "watcher_and_agents_gone": watcher_and_agents_gone,
    "apply_is_idempotent": apply_is_idempotent,
    "apply_is_cumulative": apply_is_cumulative,
    "apply_dotted_cumulative": apply_dotted_cumulative,
    "applied_entries_tracked": applied_entries_tracked,
    "help_with_skills_disabled": help_with_skills_disabled,
    "noop": noop,
    "runtime_refuses_framework_module": runtime_refuses_framework_module,
    "unknown_and_already_disabled_apply": unknown_and_already_disabled_apply,
}

SCENARIOS[sys.argv[1]]()
