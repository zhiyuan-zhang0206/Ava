"""Two hosts keep their policy inputs separate and preserve live read boundaries."""

import asyncio
from dataclasses import replace

from services.agent_runner.agent_host.runtime import HostCachePolicy, HostPolicy
from services.agent_runner.agent_host.tests.test_agent_host import _Build, _Row
from services.agent_runner.agent_host.tests.test_agent_host import host_plugin as host_plugin
from services.agent_runner.agent_host.tests.test_agent_host import wired as wired


async def test_host_capacity_is_independent_and_fixed_at_construction(wired: _Build) -> None:
    policy = HostPolicy(1, lambda: HostCachePolicy(60, 4), lambda: "alpha", lambda: "")
    first, _, _ = wired({}, policy=policy)
    second, _, _ = wired({}, policy=replace(policy, max_concurrent_turns=2))
    entered = asyncio.Event()

    async def queued() -> None:
        async with first.admission.admit(2):
            entered.set()

    async with first.admission.admit(1):
        task = asyncio.create_task(queued())
        await asyncio.sleep(0)
        assert first.admission.waiting == 1
        async with second.admission.admit(1), second.admission.admit(2):
            assert second.admission.waiting == 0
            assert not entered.is_set()
    await task
    assert entered.is_set()
    assert first.admission.limit == 1
    assert second.admission.limit == 2


async def test_hosts_read_their_live_model_defaults_and_preserve_explicit_pins(
    wired: _Build,
) -> None:
    defaults = {"first": "alpha", "second": "beta"}
    first_policy = HostPolicy(
        0, lambda: HostCachePolicy(60, 4), lambda: defaults["first"], lambda: ""
    )
    second_policy = replace(first_policy, default_model=lambda: defaults["second"])
    first, first_graph, _ = wired({1: _Row()}, policy=first_policy)
    second, second_graph, _ = wired({1: _Row()}, policy=second_policy)
    await asyncio.gather(first.run_turn(1), second.run_turn(1))
    assert first_graph.observations[-1].model == "alpha"
    assert second_graph.observations[-1].model == "beta"
    defaults["first"] = "gamma"
    first.drop_agent(1)  # Existing cache invalidation remains the model rebuild boundary.
    await asyncio.gather(first.run_turn(1), second.run_turn(1))
    assert first_graph.observations[-1].model == "gamma"
    assert first_graph.observations[-1].llm.name == "gamma"
    assert second_graph.observations[-1].model == "beta"

    def unneeded_default() -> str:
        raise AssertionError("an explicit pin must not read the default")

    pinned, graph, _ = wired(
        {1: _Row(overlay={"llm_model": "alpha"})},
        policy=replace(first_policy, default_model=unneeded_default),
    )
    await pinned.run_turn(1)
    assert graph.observations[-1].model == "alpha"
    await asyncio.gather(first.aclose(), second.aclose(), pinned.aclose())


async def test_cache_policy_reads_live_values_for_each_host(wired: _Build) -> None:
    policies = {"first": HostCachePolicy(60, 4), "second": HostCachePolicy(60, 4)}
    first_policy = HostPolicy(0, lambda: policies["first"], lambda: "alpha", lambda: "")
    first, _, _ = wired({1: _Row(), 2: _Row(), 3: _Row()}, policy=first_policy)
    second, _, _ = wired(
        {1: _Row(), 2: _Row(), 3: _Row()},
        policy=replace(first_policy, cache=lambda: policies["second"]),
    )
    for agent_id in (1, 2, 3):
        await asyncio.gather(first.run_turn(agent_id), second.run_turn(agent_id))
    policies["first"] = HostCachePolicy(60, 1)
    first._evict()
    second._evict()
    assert list(first._runtimes) == [3]
    assert list(second._runtimes) == [1, 2, 3]
    policies["second"] = HostCachePolicy(0, 4)
    second._evict()
    assert not second._runtimes
    assert list(first._runtimes) == [3]
    await asyncio.gather(first.aclose(), second.aclose())
