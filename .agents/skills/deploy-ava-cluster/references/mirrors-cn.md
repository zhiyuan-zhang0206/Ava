# Package mirrors for restricted networks

Package transport is configured independently of cluster initialization.
`scripts/mirrors/cn.env` contains ordinary package-manager environment settings:
Tsinghua TUNA for Python and Homebrew, and npmmirror for npm. Review the profile,
then export its settings for dependency acquisition and any initial frontend
build:

```bash
set -a
. scripts/mirrors/cn.env
set +a
env -u VIRTUAL_ENV uv run --no-project --python 3.12 python cli/python_install.py \
  --locked --inexact --python 3.12
```

The profile does not select a home, allocate resources, or launch a cluster.
Run normal `ava start` separately with the intended home and capabilities.
For subsequent commands, existing unit `mirror.env` files remain supported with
precedence real environment > `.env` > `mirror.env`; selecting a shell profile
does not automatically persist it into a home.

The committed `uv.lock` retains canonical PyPI origins. The dependency tool
validates it, exports exact requirements and hashes offline, and obtains those
same artifacts through the configured single index. It does not re-resolve or
rewrite the lock. Existing machine uv/pip single-index settings are recognized;
explicit environment settings win. See
[Machine Python indexes](../../../../conventions/dev-setup.md#machine-python-indexes).

Toolchain downloads and OS package repositories have separate configuration.
Acquire the approved uv and Python versions using trusted, verified transport;
a Python package mirror does not also mirror GitHub releases, Node, or the OS
package manager. Mirror availability must be verified from the target network.
