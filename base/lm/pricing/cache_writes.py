"""Normalize provider cache-creation usage without counting tokens twice."""

from collections.abc import Mapping


def _count(details: Mapping[str, object], key: str) -> int:
    count = details.get(key)
    if count is None:
        return 0
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("cache-write token counts must be integers")
    if count < 0:
        raise ValueError("cache-write token counts must be non-negative")
    return count


def cache_write_tokens(details: Mapping[str, object]) -> tuple[int, int]:
    """Return 5-minute and 1-hour cache-write counts from LangChain details.

    Anthropic's legacy aggregate ``cache_creation`` uses the default 5-minute
    TTL. New responses include the TTL breakdown; LangChain may clear the
    aggregate after copying it, so never add the aggregate to the breakdown.
    """
    generic = _count(details, "cache_creation")
    five = _count(details, "ephemeral_5m_input_tokens")
    hour = _count(details, "ephemeral_1h_input_tokens")
    if five + hour:
        if generic not in (0, five + hour):
            raise ValueError("cache-creation total disagrees with its TTL breakdown")
        return five, hour
    return generic, 0
