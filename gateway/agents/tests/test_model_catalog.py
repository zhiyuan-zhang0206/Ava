"""The picker receives distinct services, prices and sourced TPS references."""

from gateway.agents.router import get_models


def test_fast_services_have_separate_picker_prices_and_tps() -> None:
    catalog = get_models()
    standard = catalog.models["gpt-5.6-sol"]
    fast = catalog.models["gpt-5.6-sol-fast"]
    assert standard.pricing is not None and fast.pricing is not None
    assert fast.pricing.input == 8
    assert fast.reference_tps is not None
    assert fast.reference_tps.display == ">80"
    assert fast.reference_tps.source_url == "https://openai.com/api-fast-mode/"
    assert "Enterprise" in fast.reference_tps.note
    assert standard.reference_tps is None
    assert catalog.models["gpt-6.1-sol-fast"].reference_tps is None
    assert catalog.models["mimo-v2.6-pro-ultraspeed"].reference_tps is None
    assert fast.reasoning_effort_options == standard.reasoning_effort_options
    wire = catalog.model_dump(mode="json")
    assert wire["models"]["gpt-5.6-sol-fast"]["reference_tps"]["display"] == ">80"
