from typing import Annotated

from pydantic import StringConstraints

# meiva:note
# User content is stripped, must be nonempty, and is limited to one million characters; legitimate input longer than 64 KiB remains accepted.
# meiva:link ops/rpc_schemas/docs/content.meiva.okf.md
_MAX_CONTENT_CHARS = 1_000_000

UserContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=_MAX_CONTENT_CHARS),
]
