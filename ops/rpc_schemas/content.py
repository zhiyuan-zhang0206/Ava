from typing import Annotated

from pydantic import StringConstraints

# meiva:ref ops/rpc_schemas/docs/content.meiva.okf.md#content-guard
_MAX_CONTENT_CHARS = 1_000_000

UserContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=_MAX_CONTENT_CHARS),
]
