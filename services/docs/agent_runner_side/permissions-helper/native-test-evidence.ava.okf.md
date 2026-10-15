---
type: doc
title: Native helper test evidence
description: Native lifecycle coverage and the desktop effects excluded by test harnesses.
---

# Native helper test evidence

`test_helper_retirement.py` uses the live interpreter for its replacement guard;
a copied binary can fail launch constraints with a native GUI error. Its
shutdown harness compiles the real daemon and keeps socket, dispatch and wait
behavior while replacing desktop effects and removing TCC/preflight. A fresh
test binary has no Screen Recording grant. These checks do not establish live
Screen Recording permission, signing or launchd registration. Short Unix-socket
paths are deliberate.
