"""Tests for the session_name() composer in shared.cluster."""

from shared import cluster


def test_session_name_has_ava_prefix_and_no_cluster_or_machine():
    # `ava-<service>` — neither cluster nor machine is encoded: the per-home
    # the per-home session backend scopes sessions, so the name needs no further distinction.
    assert cluster.session_name("gateway") == "ava-gateway"
    assert cluster.session_name("agent-42") == "ava-agent-42"


def test_repo_reexports_session_name():
    from cli.commands._repo import session_name

    assert session_name is cluster.session_name
