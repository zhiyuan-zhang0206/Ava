"""Llm factory cases: reasoning effort dispatch."""

from __future__ import annotations

from dataclasses import fields

import pytest

from base.config import get_field, settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.factory import _resolve_override, build_chat_model
from base.lm.tests.llm_factory.factory_support import _FakeLLM, _install_fake_module


class TestResolveOverride:
    """`_resolve_override` reports resolution errors in layers: format / module / factory / return type
    raises ValueError / ImportError / AttributeError / TypeError respectively —
    letting the user see from stderr which step failed without grepping the factory source code."""

    def test_no_colon_separator_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            _resolve_override("just_a_module_no_colon", "claude-opus-4-7")

    def test_empty_module_path_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            _resolve_override(":factory", "claude-opus-4-7")

    def test_invalid_factory_identifier_raises_value_error(self) -> None:
        """factory segment is not a valid Python identifier (empty / has spaces / starts with digit)
        → ValueError. If isidentifier check is missed, getattr with an invalid name behaves
        unpredictably (depends on Python internals) — must raise early."""
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            _resolve_override("mod.path:", "claude-opus-4-7")
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            _resolve_override("mod.path:has space", "claude-opus-4-7")
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            _resolve_override("mod.path:1starts_with_digit", "claude-opus-4-7")

    def test_module_not_found_raises_import_error(self) -> None:
        """module path not found → ImportError, message indicates the override."""
        with pytest.raises(ImportError, match="cannot find module"):
            _resolve_override("definitely_does_not_exist_pkg_xyz.module:factory", "claude-opus-4-7")

    def test_factory_attribute_missing_raises_attribute_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """module found but factory_name attribute not defined → AttributeError.
        Hints that the module path is correct but the factory name is misspelled / not exported."""
        _install_fake_module(monkeypatch, "_llm_override_empty")
        with pytest.raises(AttributeError, match="has no attribute 'missing_factory'"):
            _resolve_override("_llm_override_empty:missing_factory", "claude-opus-4-7")

    def test_factory_returns_non_basechatmodel_raises_type_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """factory returns non-BaseChatModel subclass → TypeError. Prevents fake factory
        from returning a string / dict that later graph code chokes on with AttributeError,
        which is hard to locate."""
        mod = _install_fake_module(monkeypatch, "_llm_override_bad_return")

        def build(_model: str, *, agent_id: int | None) -> str:
            return "not a chat model"

        mod.__dict__["build"] = build
        with pytest.raises(TypeError, match="factory returned 'str', not a BaseChatModel"):
            _resolve_override("_llm_override_bad_return:build", "claude-opus-4-7")

    def test_success_returns_factory_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """factory returns a BaseChatModel subclass instance → _resolve_override passes through.
        The model parameter should be fed to the factory as-is (factory decides whether to use it)."""
        captured_model: list[tuple[str, int | None]] = []
        mod = _install_fake_module(monkeypatch, "_llm_override_ok")

        def build(model: str, *, agent_id: int | None) -> _FakeLLM:
            captured_model.append((model, agent_id))
            return _FakeLLM()

        mod.__dict__["build"] = build
        result = _resolve_override("_llm_override_ok:build", "claude-opus-4-7", agent_id=7)
        assert isinstance(result, _FakeLLM)
        assert captured_model == [("claude-opus-4-7", 7)]

    def test_build_chat_model_respects_override_env(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """When `AVA_LLM_OVERRIDE` env is set, `build_chat_model` short-circuits through
        `_resolve_override` — bypassing claude-* / deepseek-* prefix dispatch,
        allowing e2e tests / debugging to inject a fake LLM (without hitting the real API)."""
        mod = _install_fake_module(monkeypatch, "_llm_override_e2e")
        owners: list[int | None] = []

        def build(_model: str, *, agent_id: int | None) -> _FakeLLM:
            owners.append(agent_id)
            return _FakeLLM()

        mod.__dict__["build"] = build
        monkeypatch.setattr(settings.lm, "llm_override", "_llm_override_e2e:build")
        from base.lm.factory import build_chat_model_bound

        llm = build_chat_model(
            "claude-opus-4-7",
            agent_id=8,
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(llm, _FakeLLM)
        bound, binding = build_chat_model_bound(
            "claude-opus-4-7",
            catalog=model_catalog,
            llm_override=settings.lm.llm_override,
            overrides=ModelOverrides.from_pins(
                {field.name: get_field(field.name) for field in fields(ModelOverrides)}
            ),
        )
        assert isinstance(bound, _FakeLLM)
        assert binding is None
        assert owners == [8, None]
        with pytest.raises(ValueError, match="single-attempt"):
            build_chat_model_bound(
                "claude-opus-4-7",
                single_attempt=True,
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {field.name: get_field(field.name) for field in fields(ModelOverrides)}
                ),
            )

    def test_build_chat_model_override_failure_propagates(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        """When override resolution fails, `build_chat_model` must not silently fall back
        to the original prefix dispatch — if env is set it must take effect; failure must
        be loud, telling the user to correct the env rather than letting prod silently use
        the real LLM."""
        monkeypatch.setattr(settings.lm, "llm_override", "bad_format_no_colon")
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            build_chat_model(
                "claude-opus-4-7",
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                overrides=ModelOverrides.from_pins(
                    {field.name: get_field(field.name) for field in fields(ModelOverrides)}
                ),
            )
