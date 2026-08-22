# Attach kernel wiring

Implemented the SDK-to-kernel path for `ava.self.attach` according to the
approved attachment design. Registrations are kept as absolute paths and
optional labels until the child execution result reaches the parent
checkpoint; bytes are read only when the next idle claim packs a single human
message for the model.

The compact-halt path deliberately does not merge that execution's
registrations, while previously checkpointed attachments remain pending. This
keeps compaction recovery deterministic without placing raw media in durable
state.

The LM-specific packing policy remains in the earlier LM layer; this change
only consumes its public attachment packer and constants.
