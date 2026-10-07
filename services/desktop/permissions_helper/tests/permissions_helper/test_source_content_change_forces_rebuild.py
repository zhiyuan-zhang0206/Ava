"""Permissions helper cases: source content change forces rebuild."""

from __future__ import annotations

import plistlib
import time
from pathlib import Path

import pytest

from services.desktop.permissions_helper.tests.test_permissions_helper import (
    _TEST_CERT_SHA1,
    _argvs,
    _fake_tools,
    _install_env,
    _sign_command,
    _stage_bundle,
    _test_dr,
    _write_current_build_state,
)
from services.desktop.permissions_helper.tests.test_permissions_helper import (
    fake_helper as fake_helper,
)


def test_source_content_change_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = _fake_tools(monkeypatch, authority=lifecycle._CERT_CN)
    _write_current_build_state(app, lifecycle._source_content_hash())
    lifecycle._SOURCE.write_text("// changed swift")

    before = (app / "Contents/MacOS/AvaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign()
    assert (app / "Contents/MacOS/AvaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in _argvs(recorded))


def test_locale_content_change_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = _fake_tools(monkeypatch, authority=lifecycle._CERT_CN)
    _write_current_build_state(app, lifecycle._source_content_hash())
    locale_file = lifecycle._LOCALES / "en.lproj" / "Localizable.strings"
    locale_file.write_text('"panel.title" = "Changed";')

    before = (app / "Contents/MacOS/AvaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign()
    assert (app / "Contents/MacOS/AvaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in _argvs(recorded))


def test_entitlements_content_change_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The entitlements file is a build input: editing it must rebuild the artifact."""
    from services.desktop.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = _fake_tools(monkeypatch, authority=lifecycle._CERT_CN)
    _write_current_build_state(app, lifecycle._source_content_hash())
    lifecycle._ENTITLEMENTS.write_bytes(b"<plist><dict/></plist>")

    before = (app / "Contents/MacOS/AvaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign()
    assert (app / "Contents/MacOS/AvaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in _argvs(recorded))


def test_build_copies_locale_resources_into_the_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    _fake_tools(monkeypatch, authority=None)

    assert lifecycle.build_and_sign() == (app, True)
    for language in ("en", "zh-Hans"):
        source = lifecycle._LOCALES / (language + ".lproj") / "Localizable.strings"
        copied = app / "Contents" / "Resources" / (language + ".lproj") / "Localizable.strings"
        assert copied.is_file()
        assert copied.read_text() == source.read_text()


def test_missing_build_state_forces_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = _fake_tools(monkeypatch, authority=lifecycle._CERT_CN)

    before = (app / "Contents/MacOS/AvaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign()
    assert (app / "Contents/MacOS/AvaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in _argvs(recorded))


def test_expected_dr_uses_the_named_identity_sha1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    mixed_case_sha1 = "aBcDeF0123456789aBcDeF0123456789aBcDeF01"
    _fake_tools(
        monkeypatch,
        authority=None,
        identity_output=(
            f'  2) {mixed_case_sha1} "{lifecycle._CERT_CN}" (CSSMERR_TP_NOT_TRUSTED)\n'
        ),
    )

    assert lifecycle._expected_dr() == (
        f'identifier "{lifecycle._BUNDLE_ID}" and certificate leaf = H"{mixed_case_sha1.lower()}"'
    )


def test_expected_dr_rejects_a_missing_or_misnamed_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    _fake_tools(
        monkeypatch,
        authority=None,
        identity_output=f'  1) {_TEST_CERT_SHA1} "Some Other Identity"\n',
    )

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="missing or name mismatch"):
        lifecycle._expected_dr()


def test_verify_dr_rejects_permission_reset_risk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=True)
    _fake_tools(
        monkeypatch,
        authority=lifecycle._CERT_CN,
        designated_requirement='identifier "wrong.bundle"',
    )

    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="permissions reset risk"):
        lifecycle._verify_dr(app)


def test_identity_change_warns_and_rebuilds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = _fake_tools(monkeypatch, authority=lifecycle._CERT_CN)
    _write_current_build_state(app, "hash-for-previous-identity", dr='identifier "old"')

    before = (app / "Contents/MacOS/AvaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign()
    assert (app / "Contents/MacOS/AvaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "swiftc" for c in _argvs(recorded))
    assert (
        "code-signing identity changed — macOS permissions may need re-granting"
        in capsys.readouterr().err
    )


def test_ad_hoc_signed_bundle_is_rebuilt_onto_the_stable_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bundle an earlier build left ad-hoc carries a per-build identity whose
    grant TCC drops on every rebuild, so re-signing forfeits nothing live and is
    the only way that host returns to the identity the grants are keyed on."""
    from services.desktop.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=True)
    recorded = _fake_tools(monkeypatch, authority=None)

    before = (app / "Contents/MacOS/AvaPermissionsHelper").read_bytes()
    with pytest.raises(RuntimeError, match="still in use"):
        lifecycle.build_and_sign()
    assert (app / "Contents/MacOS/AvaPermissionsHelper").read_bytes() == before
    assert not any(c[0] == "codesign" and "--force" in c for c in _argvs(recorded))


def test_locked_keychain_refuses_instead_of_downgrading_to_ad_hoc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None, keychain_rc=1)

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.build_and_sign()
    assert "User interaction is not allowed" in str(err.value)
    assert "unlock-keychain" in str(err.value)
    assert not any(c[0] == "swiftc" for c in _argvs(recorded))
    assert not any(c[:2] == ["codesign", "--force"] for c in _argvs(recorded))


def test_codesign_failure_names_the_ad_hoc_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    _fake_tools(monkeypatch, authority=None, sign_rc=1)

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.build_and_sign()
    assert "errSecInternalComponent" in str(err.value)
    assert "unlock-keychain" in str(err.value)


def test_every_call_carries_its_declared_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None)

    lifecycle.build_and_sign()
    assert recorded  # an unbounded call site would already have KeyError'd in the fake
    for call in recorded:
        expected = (
            lifecycle._ACL_PROBE_TIMEOUT_S
            if call.argv[:2] == ["codesign", "--sign"] and Path(call.argv[-1]).name == "acl-probe"
            else lifecycle._TIMEOUTS_S[call.argv[0]]
        )
        assert call.timeout == expected, call.argv


def test_swiftc_hang_fails_the_build_instead_of_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None, hang=("swiftc",))

    with pytest.raises(lifecycle.PermissionsHelperTimeoutError) as err:
        lifecycle.build_and_sign()
    assert f"{lifecycle._TIMEOUTS_S['swiftc']:.0f}s" in str(err.value)
    assert not any(c[:2] == ["codesign", "--force"] for c in _argvs(recorded))


def test_codesign_hang_fails_with_the_acl_remedy_not_the_unlock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 2026-08-02 hang, arriving after the preflight cleared this host. It has
    to surface as a failed step, and name the key's access control: a keychain
    unlock is not what fixes a per-use confirmation prompt."""
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    _fake_tools(monkeypatch, authority=None, hang=("codesign", "--force"))

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.build_and_sign()
    assert "did not finish within" in str(err.value)
    assert "set-key-partition-list" in str(err.value)
    assert "unlock-keychain" not in str(err.value)


def test_acl_probe_precedes_the_build_and_signs_a_scratch_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None)

    assert lifecycle.build_and_sign() == (app, True)
    argvs = _argvs(recorded)
    probe = next(c for c in argvs if c[:2] == ["codesign", "--sign"])
    assert probe[2] == lifecycle._CERT_CN
    # A scratch path, never the bundle: a probe that DOES trip the dialog must not
    # be able to leave the helper half-signed.
    assert str(app) not in probe[3]
    assert argvs.index(probe) < argvs.index(next(c for c in argvs if c[0] == "swiftc"))


def test_signing_smoke_signs_scratch_and_reads_matching_requirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None)

    lifecycle.preflight_signing_smoke()

    argvs = _argvs(recorded)
    sign = next(c for c in argvs if c[:2] == ["codesign", "--sign"])
    scratch = Path(sign[-1])
    assert sign[:-1] == [
        "codesign",
        "--sign",
        lifecycle._CERT_CN,
        "-v",
        "--identifier",
        lifecycle._BUNDLE_ID,
        "--requirements",
        f"=designated => {_test_dr()}",
    ]
    read = ["codesign", "-d", "-r-", str(scratch)]
    assert argvs.index(sign) < argvs.index(read)
    assert not scratch.exists()


def test_signing_smoke_refuses_a_codesign_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    _fake_tools(monkeypatch, authority=None, smoke_sign_rc=1)

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.preflight_signing_smoke()
    assert "signing smoke failed — refusing to rebuild/deploy" in str(err.value)
    assert "errSecInternalComponent" in str(err.value)
    assert lifecycle._SIGNING_REACH_REMEDY not in str(err.value)


def test_signing_smoke_exit_25300_names_repair_and_present_search_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None, smoke_sign_rc=-25300)

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.preflight_signing_smoke()
    message = str(err.value)
    assert lifecycle._SIGNING_REACH_REMEDY in message
    assert "is present in the user keychain search list" in message
    assert ["security", "list-keychains", "-d", "user"] in _argvs(recorded)


def test_signing_smoke_item_not_found_text_names_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    _fake_tools(
        monkeypatch,
        authority=None,
        smoke_sign_rc=1,
        smoke_sign_stderr=b"The specified item could not be found: errSecItemNotFound",
    )

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.preflight_signing_smoke()
    assert lifecycle._SIGNING_REACH_REMEDY in str(err.value)


def test_signing_smoke_item_not_found_names_missing_search_list_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    _fake_tools(
        monkeypatch,
        authority=None,
        smoke_sign_rc=-25300,
        list_keychains_output=b'  "/Library/Keychains/System.keychain"  \n',
    )

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.preflight_signing_smoke()
    message = str(err.value)
    assert lifecycle._SIGNING_REACH_REMEDY in message
    assert "is missing from the user keychain search list" in message


def test_signing_smoke_item_not_found_tolerates_unreadable_search_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    _fake_tools(
        monkeypatch,
        authority=None,
        smoke_sign_rc=-25300,
        list_keychains_rc=1,
    )

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.preflight_signing_smoke()
    message = str(err.value)
    assert "signing smoke failed — refusing to rebuild/deploy" in message
    assert lifecycle._SIGNING_REACH_REMEDY in message
    assert "user keychain search list is unreadable" in message


def test_signing_smoke_refuses_when_requirement_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    _fake_tools(
        monkeypatch,
        authority=None,
        dr_streams=(b"", b"Executable=/tmp/signing-smoke\n"),
    )

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.preflight_signing_smoke()
    assert "signing smoke failed — refusing to rebuild/deploy" in str(err.value)
    assert "did not report a designated requirement" in str(err.value)


def test_rebuild_runs_signing_smoke_after_acl_probe_before_compile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None)
    smoke_call_offsets: list[int] = []
    monkeypatch.setattr(
        lifecycle,
        "preflight_signing_smoke",
        lambda: smoke_call_offsets.append(len(recorded)),
    )

    assert lifecycle.build_and_sign() == (app, True)
    argvs = _argvs(recorded)
    acl_probe_index = next(
        i
        for i, cmd in enumerate(argvs)
        if cmd[:2] == ["codesign", "--sign"] and Path(cmd[-1]).name == "acl-probe"
    )
    compile_index = next(i for i, cmd in enumerate(argvs) if cmd[0] == "swiftc")
    assert smoke_call_offsets == [compile_index]
    assert acl_probe_index < smoke_call_offsets[0]


def test_acl_prompt_refuses_before_anything_is_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None, hang=("codesign", "--sign"))

    with pytest.raises(lifecycle.PermissionsHelperBuildError) as err:
        lifecycle.build_and_sign()
    assert "SecurityAgent" in str(err.value)
    assert "set-key-partition-list" in str(err.value)
    assert not any(c[0] == "swiftc" for c in _argvs(recorded))
    assert not any(c[:2] == ["codesign", "--force"] for c in _argvs(recorded))


def test_acl_probe_nonzero_is_inconclusive_and_the_build_proceeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """codesign takes different paths for a loose file and a bundle, so a non-zero
    probe is no evidence the real sign would fail -- refusing on it would ground
    hosts that sign fine. The bound on the real sign is the backstop."""
    from services.desktop.permissions_helper import lifecycle

    app = _stage_bundle(monkeypatch, tmp_path, exe_present=False)
    recorded = _fake_tools(monkeypatch, authority=None, acl_probe_rc=1)

    assert lifecycle.build_and_sign() == (app, True)
    assert _sign_command(recorded)[3] == lifecycle._CERT_CN


def test_hung_helper_job_is_unknown_while_build_probes_report_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out native ownership query cannot certify helper absence."""
    import subprocess

    from services.desktop.permissions_helper import launchd_job, lifecycle

    def hang(cmd: list[str], *, timeout: float, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(lifecycle, "run_bounded", hang)
    monkeypatch.setattr(launchd_job, "run_bounded", hang)
    monkeypatch.setattr("base.paths.ava_home", lambda: Path("/x/.ava-demo"))

    with pytest.raises(subprocess.TimeoutExpired):
        lifecycle._is_loaded()
    assert lifecycle._signed_with_stable_cert(Path("/nope.app")) is False
    reason = lifecycle._keychain_lock_reason()
    assert reason is not None
    assert "timed out" in reason


def test_helper_ping_settles_until_cold_start_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.desktop.permissions_helper import client, lifecycle

    replies: list[object] = [
        client.PermissionsHelperError("socket not ready"),
        {},
        {"pong": False},
        {"pong": True, "root_stop_intent_v1": True, "helper_shutdown_v1": True},
    ]
    sleeps: list[float] = []

    def ping() -> dict[str, object]:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        assert isinstance(reply, dict)
        return reply

    monkeypatch.setattr(client, "ping", ping)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    assert lifecycle._helper_answers_ping()
    assert sleeps == [0.5, 0.5, 0.5]


@pytest.mark.parametrize("changed", [False, True])
def test_loaded_helper_changes_refuse_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, changed: bool
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app, run_calls, probe_calls = _install_env(
        monkeypatch, tmp_path, loaded=True, matching_plist=not changed
    )
    path = lifecycle._plist_path()
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="stop and unregister"):
        lifecycle.install_and_load(app, rebuilt=not changed)
    assert (path.read_bytes() if path.exists() else None) == before
    assert run_calls == probe_calls == []


def test_unresponsive_loaded_helper_keeps_unknown_custody(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app, run_calls, probe_calls = _install_env(monkeypatch, tmp_path, loaded=True)
    monkeypatch.setattr(lifecycle, "_helper_answers_ping", lambda: False)
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="custody is unknown"):
        lifecycle.install_and_load(app, rebuilt=False)
    assert run_calls == probe_calls == []


def test_unloaded_helper_bootstraps_and_must_answer_ping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.desktop.permissions_helper import client, lifecycle

    app, run_calls, _ = _install_env(monkeypatch, tmp_path, loaded=False, matching_plist=False)
    monkeypatch.setattr(
        client,
        "ping",
        lambda: {
            "pong": True,
            "preflight_screen": True,
            "ax_trusted": True,
            "root_stop_intent_v1": True,
            "helper_shutdown_v1": True,
        },
    )

    lifecycle.install_and_load(app, rebuilt=False)

    assert ["launchctl", "bootstrap", "gui/501", str(lifecycle._plist_path())] in run_calls
    plist = plistlib.loads(lifecycle._plist_path().read_bytes())
    assert plist["ProgramArguments"] == [str(app / "Contents" / "MacOS" / "AvaPermissionsHelper")]
    assert plist["KeepAlive"] == {"SuccessfulExit": False}


@pytest.mark.parametrize("loaded", [False, True])
def test_helper_start_clears_shutdown_intent_only_after_native_job_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, loaded: bool
) -> None:
    from services.desktop.permissions_helper import lifecycle

    app, commands, _ = _install_env(monkeypatch, tmp_path, loaded=loaded)
    monkeypatch.setattr(lifecycle, "_helper_answers_ping", lambda: True)
    marker = tmp_path / "run" / "ava-root" / "helper-stopped"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"stopped\n")
    marker.chmod(0o600)
    if loaded:
        with pytest.raises(lifecycle.PermissionsHelperBuildError, match="retirement is incomplete"):
            lifecycle.install_and_load(app, rebuilt=False)
        assert marker.read_bytes() == b"stopped\n" and commands == []
    else:
        lifecycle.install_and_load(app, rebuilt=False)
        assert not marker.exists()
        assert [cmd[1] for cmd in commands] == ["bootstrap"]
