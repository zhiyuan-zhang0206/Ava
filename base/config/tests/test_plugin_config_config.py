"""The LLM model is a per-agent setting."""


def test_llm_model_is_per_agent() -> None:
    """llm_model must be marked per_agent=True for the spawn-time overlay path."""
    from base.config import FIELD_INFOS

    info = FIELD_INFOS["llm_model"]
    extra = info.json_schema_extra
    assert isinstance(extra, dict)
    assert extra.get("per_agent") is True  # pyright: ignore[reportUnknownMemberType]
