"""Source identity and startup inputs: interpreter, configuration digest, verified reads.

``runtime_interpreter`` names the interpreter of the currently imported code;
``start_inputs`` identifies the home's authoritative startup configuration;
``verified_file`` is the bounded, stability-checked read both of them use.
``python_lock`` validates the dependency lock, ``collector_artifact`` acquires
the pinned collector, ``editable_install`` guards a checkout's editable install
and ``tags`` parses dated release tags.

Several members run on settings-free paths (CI under a bare interpreter), so
this door stays docstring-only.
"""
