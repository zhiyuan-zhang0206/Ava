"""The helper's hardened-runtime signing policy.

Signed with the hardened runtime (no dyld or library-validation exception),
dyld ignores every DYLD_* variable for the helper and maps only platform
libraries, so no injected code runs before `main` in the permission ancestor.
The designated requirement, and the TCC grants keyed on it, do not change.
"""

from __future__ import annotations

SIGNING_OPTIONS = ("--options", "runtime")
