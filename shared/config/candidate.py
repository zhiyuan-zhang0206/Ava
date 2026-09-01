"""Validate full config candidates before a write can make startup impossible.

The 2026-09-01 OSS incident persisted a partial store-backend transition that
violated ``PhysicalBackupSettings``' restore-proof invariant. New processes
then failed while constructing Settings, before they could report a result.
Write boundaries use this module to reconstruct the complete affected domain
from the fresh `.env` state plus their patch, so the same Pydantic coercion and
cross-field validators that run at startup reject an invalid candidate before
anything reaches disk.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cache
from io import StringIO
from typing import Any

from dotenv import dotenv_values
from pydantic import ValidationError
from pydantic_core import ErrorDetails, PydanticUndefined

from shared import runtime_config
from shared.config import FIELD_INFOS, field_alias
from shared.config.service_read import _domain_model_classes
from shared.config_registry import field_domain
from shared.envfile import capture_env_bytes

__all__ = [
    "EnvPatchValidation",
    "validate_env_patch",
    "validate_env_patch_for_write",
    "validate_env_patch_or_raise",
]


@dataclass(frozen=True)
class EnvPatchValidation:
    """An alias-safe candidate verdict bound to one exact `.env` image.

    A writer must pass ``expected_digest`` to ``runtime_config.write_fields``.
    That compare-and-swap rejects a config change that arrived after validation,
    so two individually valid patches cannot persist an invalid combination.
    """

    errors: list[str]
    errors_by_domain: dict[str, list[str]]
    expected_digest: str


def validate_env_patch(updates: dict[str, object], removals: set[str]) -> list[str]:
    """Return validation errors for the full `.env` candidate after this patch.

    This read-only wrapper has no persistence guarantee; writers use
    ``validate_env_patch_for_write`` and pass its digest to ``write_fields``.
    """
    return validate_env_patch_for_write(updates, removals).errors


def validate_env_patch_for_write(
    updates: dict[str, object], removals: set[str]
) -> EnvPatchValidation:
    """Validate an affected-domain candidate and bind it to its file digest.

    The exact file image is captured under the writer lock, then parsed without
    re-reading the file. Only fields belonging to a patched domain are
    reconstructed; an invalid setting in an unrelated profile can therefore
    neither crash nor reject this candidate. Fields absent from the fresh file
    retain their boot-time environment value or Pydantic default.
    """
    if not updates and not removals:
        return EnvPatchValidation(
            [], {}, _env_digest(capture_env_bytes(runtime_config.env_file_path()))
        )

    patched_domains = {field_domain(name) for name in set(updates) | removals}
    payload = capture_env_bytes(runtime_config.env_file_path())
    aliases = _env_aliases(payload)

    models = _domain_model_classes()
    errors: list[str] = []
    errors_by_domain: dict[str, list[str]] = {}
    for domain in sorted(patched_domains):
        source_model = models[domain]
        model = _candidate_validation_model(source_model)
        try:
            model.model_validate(
                _candidate_payload(source_model, source_model(), aliases, updates, removals)
            )
        except ValidationError as exc:
            domain_errors = _render_validation_errors(exc, model)
            errors.extend(domain_errors)
            errors_by_domain[domain] = domain_errors
    return EnvPatchValidation(errors, errors_by_domain, _env_digest(payload))


def _env_aliases(payload: bytes) -> dict[str, str]:
    """Parse one captured `.env` image without a second filesystem read."""
    return {
        name: value
        for name, value in dotenv_values(stream=StringIO(payload.decode())).items()
        if value is not None
    }


def _candidate_payload(
    model: type[Any],
    source: Any,
    aliases: dict[str, str],
    updates: dict[str, object],
    removals: set[str],
) -> dict[str, object]:
    """Build one complete domain payload from its captured file image and patch.

    Supplying all values makes Pydantic-settings independent from ambient source
    discovery. A missing required field stays omitted, so the init-only candidate
    model reports it instead of recovering it from ``os.environ``.
    """
    candidate: dict[str, object] = {}
    for name in model.model_fields:
        if name in updates:
            candidate[name] = updates[name]
            continue
        if name in removals:
            default = FIELD_INFOS[name].get_default(call_default_factory=True)
        else:
            alias = field_alias(name)
            if alias in aliases:
                candidate[name] = aliases[alias]
                continue
            candidate[name] = getattr(source, name)
            continue
        if default is not PydanticUndefined:
            candidate[name] = default
    return candidate


def _env_digest(payload: bytes) -> str:
    """Return the compare-and-swap digest for one exact candidate input."""
    return hashlib.sha256(payload).hexdigest()


def validate_env_patch_or_raise(updates: dict[str, object], removals: set[str]) -> None:
    """Raise ``ValueError`` when the full `.env` candidate is invalid."""
    errors = validate_env_patch(updates, removals)
    if errors:
        raise ValueError("; ".join(errors))


def _render_validation_errors(error: ValidationError, model: type[Any]) -> list[str]:
    """Render Pydantic errors with `.env` aliases and never secret values."""
    return [_render_validation_error(detail, model) for detail in error.errors()]


def _render_validation_error(detail: ErrorDetails, model: type[Any]) -> str:
    """Add the best matching `.env` alias to one Pydantic error message."""
    message = str(detail["msg"])
    names: set[str] = set()
    for location in detail["loc"]:
        if not isinstance(location, str):
            continue
        name = _field_name_for_location(location, model)
        if name is not None:
            names.add(name)
    if not names:
        names = _names_described_by_error(message, model)
    aliases = ", ".join(sorted(field_alias(name) for name in names))
    return f"{aliases}: {message}" if aliases else message


def _field_name_for_location(location: str, model: type[Any]) -> str | None:
    """Resolve either a field name or its validation alias to a field name."""
    if location in model.model_fields:
        return location
    return next(
        (name for name in model.model_fields if field_alias(name) == location),
        None,
    )


def _names_described_by_error(message: str, model: type[Any]) -> set[str]:
    """Find fields whose descriptions name a model-level invariant's subject.

    Pydantic gives cross-field model validators an empty location. Matching the
    validator's nouns to Field descriptions keeps errors actionable without
    embedding domain-specific validation rules or exposing candidate values.
    """
    terms = set(re.findall(r"[a-z0-9]+", message.lower()))
    scores = {
        name: len(terms & set(re.findall(r"[a-z0-9]+", (info.description or "").lower())))
        for name, info in model.model_fields.items()
    }
    highest = max(scores.values(), default=0)
    return {name for name, score in scores.items() if highest >= 2 and score == highest}


@cache
def _candidate_validation_model(model: type[Any]) -> type[Any]:
    """Return a Settings model that validates only explicit candidate values.

    A full payload normally prevents pydantic-settings from consulting the
    environment. A required removal deliberately omits one field, though, so
    the normal Settings source chain would refill it from ``os.environ`` and
    defeat the candidate check. The inherited model keeps every validator while
    restricting sources to the supplied init payload.
    """

    class CandidateValidationModel(model):
        @classmethod
        def settings_customise_sources(
            cls,
            _settings_cls: type[Any],
            **sources: Any,
        ) -> tuple[Any, ...]:
            return (sources["init_settings"],)

    return CandidateValidationModel
