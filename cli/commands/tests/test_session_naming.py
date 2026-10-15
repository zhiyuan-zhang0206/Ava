from base import cluster


def test_session_name_has_ava_prefix_and_no_cluster_or_machine():
    assert cluster.session_name("gateway") == "ava-gateway"
    assert cluster.session_name("agent-42") == "ava-agent-42"
