from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

DOCUMENT_FILENAME = "to_user.md"
ResourceId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ToUserResource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: ResourceId
    name: str
    markdown: bool
    version: str | None


class ToUserDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str | None
    version: str | None
    modified_at: datetime | None
    resources: dict[str, ToUserResource] = Field(default_factory=dict)
    legacy_html: bool = False


class ToUserReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: int = Field(gt=0)
    references: list[ResourceId] = Field(default_factory=list)
