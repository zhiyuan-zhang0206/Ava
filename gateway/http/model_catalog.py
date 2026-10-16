import logging
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from base.lm.catalog import ModelCatalog

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogLoadFailure:
    error: Exception


class ModelCatalogUnavailableError(RuntimeError):
    pass


# ava:ref gateway/http/docs/model-catalog.ava.okf.md#retained-catalog-failure
def load_model_catalog(build: Callable[[], ModelCatalog]) -> ModelCatalog | CatalogLoadFailure:
    try:
        return build()
    except Exception as error:
        _log.exception("Model catalog initialization failed; gateway management remains available")
        return CatalogLoadFailure(error)


def require_catalog(result: ModelCatalog | CatalogLoadFailure) -> ModelCatalog:
    if isinstance(result, CatalogLoadFailure):
        raise ModelCatalogUnavailableError(
            "Model catalog is unavailable. Repair the provider configuration and restart the gateway."
        ) from result.error
    return result


def require_model_catalog(request: Request) -> ModelCatalog:
    return require_catalog(request.app.state.catalog)


def catalog_for_observation(result: ModelCatalog | CatalogLoadFailure) -> ModelCatalog | None:
    return None if isinstance(result, CatalogLoadFailure) else result
