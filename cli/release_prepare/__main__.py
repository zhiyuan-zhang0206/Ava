"""Build one inactive image; acquiring inputs and activating it are separate."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from cli.release_prepare import LocalInputs, Preparation, prepare_image
from cli.release_prepare.models import encode
from shared.verified_file import regular_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--commit", required=True, help="Exact committed source SHA")
    parser.add_argument("--work", type=Path, required=True, help="New exclusive evidence directory")
    parser.add_argument(
        "--store", type=Path, required=True, help="Existing private releases directory"
    )
    parser.add_argument("--inputs", type=Path, required=True, help="Explicit LocalInputs JSON")
    args = parser.parse_args()
    try:
        supplied = LocalInputs.model_validate_json(regular_bytes(args.inputs))
        request = Preparation(
            repo=args.repo, commit=args.commit, work=args.work, store=args.store, inputs=supplied
        )
        receipt = prepare_image(request)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"release preparation failed: {exc}\n")
        return 1
    sys.stdout.write(encode(receipt).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
