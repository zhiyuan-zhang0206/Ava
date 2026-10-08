"""The picker receives distinct services, prices and sourced TPS references."""

from fastapi.testclient import TestClient

from gateway.app import app
from gateway.schemas.models import ModelsResponse


def get_models() -> ModelsResponse:
    with TestClient(app) as client:
        response = client.get("/api/models")
    assert response.status_code == 200, response.text
    return ModelsResponse.model_validate(response.json())


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


def test_picker_exposes_declared_fast_relationships() -> None:
    catalog = get_models()
    assert catalog.models["gpt-5.6-sol-fast"].fast_of == "gpt-5.6-sol"
    assert catalog.models["gpt-5.6-sol"].fast_of is None
    # Xiaomi's native SKU does not use a speed switch or a served-speed receipt.
    assert catalog.models["mimo-v2.6-pro-ultraspeed"].fast_of is None


def test_picker_exposes_served_service_cache_write_rates() -> None:
    catalog = get_models()
    standard = catalog.models["claude-opus-5-5"].pricing
    fast = catalog.models["claude-opus-5-5-fast"].pricing
    assert standard is not None and fast is not None
    assert (standard.cache_write_5m, standard.cache_write_1h) == (5, 8)
    assert (fast.cache_write_5m, fast.cache_write_1h) == (10, 16)
