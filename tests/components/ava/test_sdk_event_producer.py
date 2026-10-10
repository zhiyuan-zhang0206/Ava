"""SDK recorders capture cold producers with their call-local identity."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import ava
from ava.sdk_surface import metering
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.agents.sdk import call_policy
from base.telemetry import emit
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import DrainStatus, Event


@pytest.fixture
def sampling(monkeypatch: pytest.MonkeyPatch) -> Iterator[call_policy.SamplingPolicyOwner]:
    owner = call_policy.SamplingPolicyOwner()

    def policy(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", policy)
    yield owner
    owner.close()


@pytest.fixture
def bare_context() -> Iterator[None]:
    prior = getattr(ava, "context", None)
    ava.unbind_context()
    try:
        yield
    finally:
        if prior is None:
            ava.unbind_context()
        else:
            ava.bind_context(prior)


def test_bare_python_recorder_uses_its_captured_cold_producer(
    bare_context: None, sampling: call_policy.SamplingPolicyOwner
) -> None:
    order: list[str] = []
    written: list[Event] = []
    built: list[EventPipeline] = []

    def producer() -> EventPipeline:
        order.append("producer")
        pipe = EventPipeline(writer=written.extend, batch_size=1)
        built.append(pipe)
        return pipe

    def body() -> str:
        order.append("body")
        return "done"

    wrapped = metering._make_recorder(body, "probe.bare", sampling, producer)
    assert order == [] and built == []
    try:
        assert wrapped() == "done"
        assert order == ["body", "producer"]
        assert built[0].sync(timeout=1).status is DrainStatus.COMPLETED
        assert written[0].source == "system" and written[0].agent_id is None
        assert written[0].attributes["fn"] == "probe.bare"
    finally:
        for pipe in built:
            assert pipe.stop(timeout=1).status is DrainStatus.COMPLETED


def test_sampled_out_call_does_not_construct_a_producer(
    bare_context: None, monkeypatch: pytest.MonkeyPatch, sampling: call_policy.SamplingPolicyOwner
) -> None:
    def policy(_owner: call_policy.SamplingPolicyOwner) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy(sampling_enabled=True, sample_every=2)

    def sampled_out(_every: int) -> int:
        return 1

    monkeypatch.setattr(call_policy, "policy", policy)
    monkeypatch.setattr("random.randrange", sampled_out)

    def forbidden() -> EventPipeline:
        pytest.fail("sampled-out SDK call built a writer")

    wrapped = metering._make_recorder(lambda: "done", "probe.sample", sampling, forbidden)
    assert wrapped() == "done"


def test_capture_failure_prevents_lazy_producer_construction() -> None:
    original = ValueError("durable capture failed")

    def capture(event: Event) -> Event:
        raise original

    def forbidden() -> EventPipeline:
        pytest.fail("producer was constructed before durable capture")

    with pytest.raises(ValueError) as observed:
        emit(
            "telemetry", "sdk_call", attributes={"fn": "probe"}, capture=capture, producer=forbidden
        )
    assert observed.value is original


def test_context_switch_in_body_does_not_change_the_captured_producer_or_identity(
    monkeypatch: pytest.MonkeyPatch, sampling: call_policy.SamplingPolicyOwner
) -> None:
    written: list[Event] = []
    built: list[EventPipeline] = []

    def producer() -> EventPipeline:
        pipe = EventPipeline(writer=written.extend, batch_size=1)
        built.append(pipe)
        return pipe

    def forbidden() -> EventPipeline:
        pytest.fail("the call switched its admitted producer to the new context")

    first = ClientSet(pipeline_factory=producer)
    second = ClientSet(pipeline_factory=forbidden)
    old_context = AvaContext(identity=AgentIdentity(6111, False), clients=first)
    new_context = AvaContext(identity=AgentIdentity(6112, False), clients=second)
    monkeypatch.setattr(ava, "context", old_context)

    def body() -> None:
        ava.bind_context(new_context)

    wrapped = metering._make_recorder(body, "probe.switch", sampling, forbidden)
    try:
        wrapped()
        assert first.sync_events(timeout=1).status is DrainStatus.COMPLETED
        assert len(built) == 1 and len(written) == 1
        assert written[0].agent_id == 6111 and written[0].source == "agent:6111"
    finally:
        first.close(pipeline_timeout=1)
        second.close(pipeline_timeout=1)


def test_first_bound_plugin_load_does_not_lend_attachment_writer_to_bare_calls(
    bare_context: None,
    monkeypatch: pytest.MonkeyPatch,
    sampling: call_policy.SamplingPolicyOwner,
) -> None:
    import atexit

    from agent import extensions
    from ava.sdk_surface import install, process_context
    from base.config import ConfigBoot
    from base.config.service_read import ConfigAuthority
    from base.lm.catalog import ModelCatalog

    prior = install.uninstall()
    install.clear_load_attempt()
    written: list[Event] = []
    made: list[EventPipeline] = []
    exits: list[object] = []
    attachment = ClientSet(pipeline_factory=lambda: pytest.fail("closed attachment was borrowed"))

    def pipeline() -> EventPipeline:
        pipe = EventPipeline(writer=written.extend, batch_size=1)
        made.append(pipe)
        return pipe

    bare_clients = ClientSet(pipeline_factory=pipeline)

    def clients(*, config: ConfigBoot) -> ClientSet:
        return bare_clients

    def register(callback: object) -> object:
        exits.append(callback)
        return callback

    def load(
        *,
        surface: bool,
        catalog: ModelCatalog,
        authority: ConfigAuthority,
        producer: object,
        clock_factory: object,
    ) -> None:
        # Capture the production root inputs without installing unrelated plugins.
        captured.append(producer)

    captured: list[object] = []
    monkeypatch.setattr(process_context, "process_clients", clients)
    monkeypatch.setattr(extensions, "load_extensions", load)
    monkeypatch.setattr(atexit, "register", register)
    ava.bind_context(
        AvaContext(clients=attachment, identity=AgentIdentity(agent_id=6111, owns_loop=False))
    )
    try:
        ava.ensure_plugins_loaded(config=ConfigBoot())
        assert captured == [bare_clients.event_pipeline]
        assert exits == [bare_clients.close]
        assert made == []
        attachment.close()
        ava.unbind_context()
        wrapped = metering._make_recorder(
            lambda: "done", "probe.detached", sampling, bare_clients.event_pipeline
        )
        assert wrapped() == "done"
        assert len(made) == 1
        assert bare_clients.sync_events(timeout=1).status is DrainStatus.COMPLETED
        assert written[0].source == "system"
        assert written[0].agent_id is None
    finally:
        bare_clients.close()
        install.uninstall()
        install.clear_load_attempt()
        if prior is not None:
            install.install(
                prior.registry,
                catalog=prior.catalog,
                authority=prior.authority,
                delivery_sender=prior.delivery_sender,
                sampling=prior.sampling,
                clock_factory=prior.clock_factory,
                producer=prior.producer,
            )
