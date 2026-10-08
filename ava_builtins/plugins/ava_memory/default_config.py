"""Memory-owned service configuration, independent of agent prompt injection."""

from pydantic import BaseModel, ConfigDict, Field

from base.packages.plugins.extensions import PluginContributions


class MemoryConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    indexer_enabled: bool = Field(
        default=True,
        strict=True,
        description="Run the gateway memory indexer, even when memory index injection is disabled.",
        json_schema_extra={
            "scope": "host",
            "writable": True,
            "remote_writable": False,
            "restart_required": "gateway",
        },
    )


def contribute() -> PluginContributions:
    """Declare the memory service config without loading the agent SDK."""
    return PluginContributions(config=MemoryConfig)
