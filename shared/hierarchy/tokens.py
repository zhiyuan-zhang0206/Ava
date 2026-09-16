"""Token accounting for the hierarchy engine.

The [5,15] fan-in window and the <= 1/10 narrative ratio are fixed contract
numbers (user-confirmed 2026-09-14), calibrated in ``o200k_base`` tokens — the
counter the pilot and its machine checks used (task #3704). The counter is
deliberately NOT a per-model context measure (that is `shared/lm/context_budget`'s
job): the ratio must mean the same thing for every agent, whichever model wrote
the text. One lazy load per process; the vocab file is cached by tiktoken.
"""

from __future__ import annotations

from functools import cache

import tiktoken

# o200k_base: the tokenizer family the ratio calibration and the demo's
# machine checks were measured with (CJK runs ~0.7-0.8 tokens/char).
_ENCODING_NAME = "o200k_base"


@cache
def _encoder() -> tiktoken.Encoding:
    """The calibration encoder, loaded once per process (vocab file cached)."""
    return tiktoken.get_encoding(_ENCODING_NAME)


def count_tokens(text: str) -> int:
    """The token count of ``text`` under the calibration encoder."""
    return len(_encoder().encode(text))
