"""Shared driver for the web-ai skill family (console / deep-research / media).

`webchat.py` is the public driver the three sibling skills' `reference/*.py`
CLIs import; `_dom.py` / `_sites.py` / `_utils.py` are its own private leaf
helpers, used only by `webchat.py` itself.
"""
