from __future__ import annotations

import pytest
import zstandard

from services.pitr.base_restore_crypto import (
    BaseRestoreError,
    _decompressed_chunks,
)


def test_decompression_rejects_trailing_frame_and_expansion_over_budget() -> None:
    compressor = zstandard.ZstdCompressor()
    first = compressor.compress(b"first")
    second = compressor.compress(b"second")

    with pytest.raises(BaseRestoreError, match="trailing"):
        b"".join(_decompressed_chunks(iter((first + second,)), maximum=100))

    with pytest.raises(BaseRestoreError, match="hard stream bound"):
        b"".join(_decompressed_chunks(iter((compressor.compress(b"large"),)), maximum=2))
