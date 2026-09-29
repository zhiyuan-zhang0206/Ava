"""Native Windows sessions: private-console launch, logon identity, the resident steward.

`winproc` hosts private hidden-console sessions and the graceful-stop control
channel; `logon_session` compares Windows logon-session numbers so a caller
knows whether it shares a console session with its target; `steward` is the
resident cross-session control process `winproc` spawns per session so a
same-session helper can always deliver the graceful signal; `console_signal`
is the one-shot isolated helper both `winproc` and `steward` invoke to
actually attach and signal a console. `terminal/` holds the explicit terminal
resources these hosts allocate. Every module here is import-safe on any
platform (they wrap `ctypes.WinDLL`, not import it conditionally); only their
Windows API calls refuse off Windows.
"""
