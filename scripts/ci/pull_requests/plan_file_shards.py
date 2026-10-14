"""Collect the backend test population and write the file-shard plan (the plan step of the shard jobs).

`python -u -m scripts.ci.pull_requests.plan_file_shards --count N --plan PATH --stacks PATH`, from the
repository root. Collection runs under `periodic_tracebacks`, which writes the stacks of a hung
collection to `--stacks`; the exit status is pytest's.
"""

import argparse
import sys
from pathlib import Path

from scripts.ci.collection_tracebacks import periodic_tracebacks


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, required=True, help="number of shards to plan")
    parser.add_argument("--plan", required=True, help="where pytest writes the plan (JSON)")
    parser.add_argument("--stacks", required=True, help="where periodic tracebacks are written")
    args = parser.parse_args(argv)
    with Path(args.stacks).open("w") as stacks, periodic_tracebacks(stacks):
        import pytest

        return int(
            pytest.main(
                [
                    "-c",
                    "pyproject.toml",
                    "--rootdir=.",
                    "--collect-only",
                    "-qq",
                    "--ignore=tests/e2e",
                    "-m",
                    "not flaky",
                    "--omit-static-tests",
                    "-o",
                    "junit_family=xunit1",
                    "-o",
                    "faulthandler_timeout=300",
                    "-p",
                    "scripts.ci.file_shard_shadow",
                    f"--file-shard-count={args.count}",
                    f"--file-shard-plan={args.plan}",
                ]
            )
        )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
