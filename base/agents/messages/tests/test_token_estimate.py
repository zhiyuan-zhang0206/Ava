from base.agents.messages.token_estimate import estimate_text_tokens


def test_character_weights_cover_unicode_and_keep_whitespace_free() -> None:

    for start in range(0, 0x110000, 4096):
        text = "".join(map(chr, range(start, min(start + 4096, 0x110000))))
        cjk = sum(
            0x3000 <= ord(char) <= 0x9FFF
            or 0xAC00 <= ord(char) <= 0xD7AF
            or 0xFF00 <= ord(char) <= 0xFFEF
            for char in text
        )
        other = sum(not char.isspace() for char in text) - cjk
        assert estimate_text_tokens(text) == 0.95 * cjk + 0.36 * other


def test_large_code_and_multilingual_runs_preserve_weight() -> None:
    for text, weight in (("a" * 100_000, 36_000), ("\u6587" * 100_000, 95_000)):
        assert estimate_text_tokens(text) == weight
        assert estimate_text_tokens(" \t\r\n" + text + "\u2003\u00a0") == weight
