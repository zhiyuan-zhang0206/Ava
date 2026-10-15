import sys

if __name__ == "__main__":
    if sys.argv[1] == "get":
        from cli.commands.management.config import cmd_config_get

        raise SystemExit(cmd_config_get(None, None, local=True))
    elif sys.argv[1] == "set":
        from cli.commands.management.config import cmd_config_set

        raise SystemExit(cmd_config_set([sys.argv[2]], None, local=True))
    else:
        raise SystemExit(f"unknown verb {sys.argv[1]!r}")
