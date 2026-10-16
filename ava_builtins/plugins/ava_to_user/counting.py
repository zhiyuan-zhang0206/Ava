from __future__ import annotations

import re

_CJK_CHARACTER = re.compile(
    r"[\u3040-\u30ff"
    r"\u3400-\u4dbf"
    r"\u4e00-\u9fff"
    r"\uac00-\ud7af"
    r"\uf900-\ufaff]"
)

_WORD_RUN = re.compile(r"[^\W_]+")


# ava:ref ava_builtins/plugins/ava_to_user/docs/ava_to_user.ava.okf.md#cjk-aware-count
def count_panel_words(text: str) -> int:
    without_cjk = _CJK_CHARACTER.sub(" ", text)
    return len(_CJK_CHARACTER.findall(text)) + len(_WORD_RUN.findall(without_cjk))
