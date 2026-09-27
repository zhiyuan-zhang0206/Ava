"""Shared logic for the gmail skill's `reference/feed.py` CLI.

`imap.py` (Keychain auth, IMAP connection/search/fetch, content extraction)
and `smtp.py` (compose/send/reply/forward, IMAP-APPEND drafts) hold the two
halves of the Gmail driver that `feed.py` imports and exposes as one CLI.
"""
