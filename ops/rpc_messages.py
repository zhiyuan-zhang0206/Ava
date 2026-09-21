"""Chat request wire models shared by gateway and agent-runner callers."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ops.rpc_completion import CompletionNoticeIn
from shared.envelope import validate_source


class TextContentBlock(BaseModel):
    """Text part of a multimodal chat message (OpenAI content-block shape)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str


class ImageUrlRef(BaseModel):
    """The `image_url` object of an image content block."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)


class ImageUrlContentBlock(BaseModel):
    """Image part of a multimodal chat message (OpenAI content-block shape).

    `image_url.url` must reference an upload of the target agent
    (`/api/agents/{id}/uploads/<name>`); the message endpoint validates the
    ownership + that the file still exists on disk and 422s otherwise. Only a
    reference is carried on the wire, never base64 — the claim node inlines the
    bytes as native model content at delivery time.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["image_url"]
    image_url: ImageUrlRef


ContentBlock = Annotated[
    TextContentBlock | ImageUrlContentBlock,
    Field(discriminator="type"),
]

_MessageContent = (
    Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    | Annotated[list[ContentBlock], Field(min_length=1)]
)


class AgentMessageIn(BaseModel):
    """POST /api/agents/{id}/messages request body — for SDK send_message and
    the `ava agents send` CLI (which the generated background-run / watcher
    completion notices invoke).

    Pure INSERT + return — the caller does not inspect status.
    Auto-resurrect on the gateway side ensures delivery to any agent,
    terminated or not.

    `source` is required — the SDK passes f"agent:{my_id}", the generated
    notices pass shell:N / watcher:N; there is no default to prevent callers
    from forgetting and having inbounds silently tagged as "user", muddying
    envelope labels. The valid set is in `shared/envelope.py:validate_source`
    (system / agent:N / user / ui:page:<name> / watcher:N / shell:N /
    schedule:N); an illegal source is intercepted by 422 at the HTTP layer —
    otherwise it would land in inbound_messages and the agent claim node
    would hit ValueError and kill the process.
    """

    content: _MessageContent
    source: str = Field(min_length=1, max_length=64)
    completion_notice: CompletionNoticeIn | None = None

    @field_validator("source")
    @classmethod
    def check_envelope_source(cls, value: str) -> str:
        """Reject untrusted message sources before durable admission."""
        validate_source(value)
        return value

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        """Require meaningful content and a trusted completion-marker shape."""
        if isinstance(self.content, list):
            has_image = any(isinstance(block, ImageUrlContentBlock) for block in self.content)
            has_text = any(
                isinstance(block, TextContentBlock) and block.text.strip() for block in self.content
            )
            if not has_image and not has_text:
                raise ValueError("content blocks must include an image or non-empty text")
        if self.completion_notice is not None:
            if not isinstance(self.content, str):
                raise ValueError("completion notices require string content")
            if not (self.source.startswith("shell:") or self.source.startswith("watcher:")):
                raise ValueError("completion notices require a shell:N or watcher:N source")
        return self
