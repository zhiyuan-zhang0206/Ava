"""Fresh-interpreter entry for the standalone `--publish-offsite` tests.

Usage: `child_offsite_entry.py <configured|unconfigured> <bucket> <main args...>`
where `<bucket>` is `real` (leave `open_bucket` alone), `fake` (a working fake
bucket) or `init_failure` (a fake bucket whose initiate call fails). Runs
`services.backup.dump._main` with the given args and exits with its code.
"""

import sys

from services.backup.artifact import offsite
from services.backup.artifact.tests.oss_fake import FakeOssBucket
from services.backup.dump import _main

if __name__ == "__main__":
    configured, bucket_kind, *args = sys.argv[1:]
    if configured == "configured":

        def configured_target(**_readers: object) -> offsite.OssTarget:
            return offsite.OssTarget("https://oss.example", "b", None)  # pyright: ignore[reportArgumentType]

        offsite.configured_target = configured_target
    elif configured != "unconfigured":
        raise SystemExit(f"unknown configuration {configured!r}")
    if bucket_kind == "fake":
        offsite.open_bucket = lambda _target: FakeOssBucket()
    elif bucket_kind == "init_failure":
        failing = FakeOssBucket()
        failing.fail_init = (500, "InternalError")
        offsite.open_bucket = lambda _target: failing
    elif bucket_kind != "real":
        raise SystemExit(f"unknown bucket {bucket_kind!r}")
    raise SystemExit(_main(args))
