from base import cluster


def test_session_name_has_meiva_prefix_and_no_cluster_or_machine():
    assert cluster.session_name("gateway") == "meiva-gateway"
    assert cluster.session_name("agent-42") == "meiva-agent-42"
