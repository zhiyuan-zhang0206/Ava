"""Image-exec handoff: the only contract that crosses release versions (frozen v1).

The previous image verifies a prepared image in its home's store and runs a
fixed entry point of that image: `handoff` from the CLI, the
`release_image_exec` ops kind (`ops.cluster`) from a coordinator. The
candidate image's entry point (`__main__`) does everything else. The envelope,
exec argv and wire models are `base.api_contracts.release_handoff`.
"""
