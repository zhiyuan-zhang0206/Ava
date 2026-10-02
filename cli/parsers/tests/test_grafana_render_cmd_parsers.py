"""The `lgtm render` parser flags."""

from __future__ import annotations

from cli.parsers import build_parser


def test_lgtm_render_parser_flags() -> None:
    args = build_parser().parse_args(["lgtm", "render", "--force", "--repo-only"])
    assert args.lgtm_cmd == "render"
    assert args.force is True
    assert args.repo_only is True
    assert args.func.__name__ == "_h_lgtm"
