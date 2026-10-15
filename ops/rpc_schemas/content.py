from typing import Annotated

from pydantic import StringConstraints

# ava:ref ops/rpc_schemas/docs/content.ava.okf.md#content-guard
_MAX_CONTENT_CHARS = 1_000_000

UserContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=_MAX_CONTENT_CHARS),
]
