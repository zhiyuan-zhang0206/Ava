"""Release images: verification, preparation, loaded-runtime identity, verified reads.

``runtime_release`` verifies a release image without runtime settings and
selects a generation atomically; ``runtime_prepare`` prepares a generation
offline; ``runtime_interpreter`` names the interpreter of the currently imported
code; ``identity`` reads the builder-embedded application identity;
``runtime_publication_input`` and ``runtime_service_identity`` report the loaded
image's local facts; ``start_inputs`` identifies the home's authoritative
startup configuration; ``operation`` gates startup while a release executor owns
the home's transition; ``verified_file`` is the bounded, stability-checked
read every one of them uses. ``python_lock`` validates the dependency lock,
``collector_artifact`` acquires the pinned collector, ``editable_install``
guards a checkout's editable install and ``tags`` parses dated release tags.

Several members run on settings-free paths (release preparation, the handoff
entry, CI under a bare interpreter), so this door stays docstring-only.
"""
