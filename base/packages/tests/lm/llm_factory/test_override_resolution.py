from __future__ import annotations

import pytest

from base.config.tests.configuration import model_overrides
from base.lm.catalog import ModelCatalog
from base.lm.factory import build_chat_model, resolve_override
from base.packages.tests.lm.llm_factory.factory_support import FakeLLM, install_fake_module


class TestResolveOverride:
    def test_no_colon_separator_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            resolve_override("just_a_module_no_colon", "claude-opus-4-7")

    def test_empty_module_path_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            resolve_override(":factory", "claude-opus-4-7")

    def test_invalid_factory_identifier_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            resolve_override("mod.path:", "claude-opus-4-7")
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            resolve_override("mod.path:has space", "claude-opus-4-7")
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            resolve_override("mod.path:1starts_with_digit", "claude-opus-4-7")

    def test_module_not_found_raises_import_error(self) -> None:
        with pytest.raises(ImportError, match="cannot find module"):
            resolve_override("definitely_does_not_exist_pkg_xyz.module:factory", "claude-opus-4-7")

    def test_factory_attribute_missing_raises_attribute_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_module(monkeypatch, "tests._llm_override_empty")
        with pytest.raises(AttributeError, match="has no attribute 'missing_factory'"):
            resolve_override("tests._llm_override_empty:missing_factory", "claude-opus-4-7")

    def test_factory_returns_non_basechatmodel_raises_type_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = install_fake_module(monkeypatch, "tests._llm_override_bad_return")

        def build(_model: str, *, agent_id: int | None) -> str:
            return "not a chat model"

        mod.__dict__["build"] = build
        with pytest.raises(TypeError, match="factory returned 'str', not a BaseChatModel"):
            resolve_override("tests._llm_override_bad_return:build", "claude-opus-4-7")

    def test_success_returns_factory_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured_model: list[tuple[str, int | None]] = []
        mod = install_fake_module(monkeypatch, "tests._llm_override_ok")

        def build(model: str, *, agent_id: int | None) -> FakeLLM:
            captured_model.append((model, agent_id))
            return FakeLLM()

        mod.__dict__["build"] = build
        result = resolve_override("tests._llm_override_ok:build", "claude-opus-4-7", agent_id=7)
        assert isinstance(result, FakeLLM)
        assert captured_model == [("claude-opus-4-7", 7)]

    def test_build_chat_model_respects_override_env(
        self, monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
    ) -> None:
        mod = install_fake_module(monkeypatch, "tests._llm_override_e2e")
        owners: list[int | None] = []

        def build(_model: str, *, agent_id: int | None) -> FakeLLM:
            owners.append(agent_id)
            return FakeLLM()

        mod.__dict__["build"] = build
        from base.lm.factory import build_chat_model_bound

        llm = build_chat_model(
            "claude-opus-4-7",
            agent_id=8,
            catalog=model_catalog,
            llm_override="tests._llm_override_e2e:build",
            overrides=model_overrides(),
        )
        assert isinstance(llm, FakeLLM)
        bound, binding = build_chat_model_bound(
            "claude-opus-4-7",
            catalog=model_catalog,
            llm_override="tests._llm_override_e2e:build",
            overrides=model_overrides(),
        )
        assert isinstance(bound, FakeLLM)
        assert binding is None
        assert owners == [8, None]
        with pytest.raises(ValueError, match="single-attempt"):
            build_chat_model_bound(
                "claude-opus-4-7",
                single_attempt=True,
                catalog=model_catalog,
                llm_override="tests._llm_override_e2e:build",
                overrides=model_overrides(),
            )

    def test_build_chat_model_override_failure_propagates(
        self, *, model_catalog: ModelCatalog
    ) -> None:
        with pytest.raises(ValueError, match=r"requires 'module\.path:factory_name' form"):
            build_chat_model(
                "claude-opus-4-7",
                catalog=model_catalog,
                llm_override="bad_format_no_colon",
                overrides=model_overrides(),
            )
