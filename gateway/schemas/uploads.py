"""file upload.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from pydantic import (
    BaseModel,
    ConfigDict,
)


class UploadedFile(BaseModel):
    """Single file upload result.

    `url` is the HTTP path the saved file is served back at
    (`/api/agents/{id}/uploads/<name>`) — the frontend uses it as an image
    thumbnail src and, for a native image attachment, as the `image_url.url`
    reference it sends in the next multimodal message.
    """

    model_config = ConfigDict(frozen=True)

    filename: str
    path: str
    url: str
    size: int
    content_type: str


class UploadedBatch(BaseModel):
    """Result of one upload request — the files saved in a single batch."""

    model_config = ConfigDict(frozen=True)

    files: list[UploadedFile]
