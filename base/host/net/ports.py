"""`UrlOpener`: the port for an HTTP request whose answer a test must control.

A component that makes an HTTP request takes `open_url: UrlOpener` as a required argument (no
default) and calls it instead of `urllib.request.urlopen`. The composition root passes
`SYSTEM_URL_OPENER`; a test passes `FakeUrlOpener` from `base/host/net/tests/fake_url_opener.py`,
which answers by URL prefix and refuses a URL nobody scripted, or it starts a real listener on
port 0 and uses the system opener. A test never patches `urllib.request.urlopen` or `socket`.

The shape is `urlopen`'s own: a request (or a URL) and a timeout in, a readable context manager
out. As with `urlopen`, a status outside 2xx raises `urllib.error.HTTPError`.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

type UrlOpener = Callable[[str | urllib.request.Request, float], AbstractContextManager[Any]]


def _urlopen(target: str | urllib.request.Request, timeout: float) -> AbstractContextManager[Any]:
    return urllib.request.urlopen(target, timeout=timeout)  # noqa: S310 — a pass-through; each caller owns the validation of its URL


SYSTEM_URL_OPENER: UrlOpener = _urlopen
