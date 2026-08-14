"""composer commands.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from pydantic import (
    BaseModel,
    ConfigDict,
)


class CommandItem(BaseModel):
    """One composer command, as surfaced in the `/`-autocomplete: name +
    description + `instruction_hint` (placeholder for the single natural-language
    instruction typed after the name). The body is intentionally absent —
    expansion happens server-side in the agent's claim node, so the template
    never reaches the browser."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    instruction_hint: str
