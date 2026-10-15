import sys

from cli.parsers import command_options, parse_args

if __name__ == "__main__":
    assert command_options()
    assert callable(parse_args(["status"]).func)
    assert "base.config" not in sys.modules
    assert "cli.commands.lifecycle.status" not in sys.modules
