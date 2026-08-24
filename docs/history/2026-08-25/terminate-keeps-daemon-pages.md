# Termination keeps daemon-managed pages

Agent termination closes only `ava.ui.show()` rows. `ava.ui.serve()` and
`serve_markdown()` rows carry `serve_dir`, are owned by the page-server daemon,
and stay open with their persistent page sessions after the agent process exits.

The termination trigger and the pre-termination PageClosed-event query share
the same `serve_dir IS NULL` ownership boundary. The existing resurrect trigger
is unchanged: it reopens only show() rows that termination closed; daemon rows
never need reopening.
