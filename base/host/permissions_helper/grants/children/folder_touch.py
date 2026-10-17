import sys
from pathlib import Path


def main() -> None:
    target, result_path = Path(sys.argv[1]), Path(sys.argv[2])
    try:
        names = list(target.iterdir())
        text = f"granted {len(names)}"
    except PermissionError:
        text = "denied"
    except Exception as exc:
        text = f"error {type(exc).__name__}: {exc}"
    with result_path.open("w") as handle:
        handle.write(text)


if __name__ == "__main__":
    main()
