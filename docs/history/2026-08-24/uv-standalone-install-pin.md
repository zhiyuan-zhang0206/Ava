# uv standalone install pin

The astral installer followed a rolling latest release without verifying its
checksum. It was the last unpinned uv install path after the brew pin manifest
and CI setup-uv versions were pinned by the brew-pin-hardening work.

`scripts/provision/toolchain.sh` now downloads the uv 0.10.2 release asset for
the host platform and refuses to install it when its sha256 does not match the
approved value. `shared/brew_pin.py` is the canonical pin; toolchain.sh embeds
the same values because it runs before Python exists, and a contract test keeps
the copies consistent.

`UV_RELEASE_BASE_URL` remains overridable for trusted mirror proxies. The
checksum verification still applies, so changing the download route does not
weaken the pin.
