---
type: doc
title: LM Media Capabilities and Attachments
description: Per-model native-media resolution and turn-boundary attachment packing.
tags:
- shared
- library
- llm-inference
---

# LM Media Capabilities and Attachments

`ModelSpec.media_types` declares a registered model's native `image`, `pdf`,
`audio`, and `video` support; an empty set means text-only. It replaces the
per-model `vision` bool while leaving the provider-plugin v1 binding contract
unchanged.

`factory.media_types_for_model()` resolves one answer in order: a registered
core or plugin model's `ModelSpec.media_types`; an unregistered plugin's
`ProviderBinding.vision` (image-only); then the core `_VISION_MODEL_PREFIXES`
fallback (image-only). No match is text-only. `model_supports_vision()` is the
message-endpoint image gate derived from that result.

`factory.attach_modalities_for_model()` is the attach gate: `ModelSpec.attach_modalities`
when the entry declares an attach-specific opinion (must be a subset of `media_types`,
enforced by the registry), else the native `media_types` — attach registers files into
the same message pipeline, so the native matrix is the default contract (ruling
2026-08-28). An empty result means attach is unavailable: `ava.self.attach` is hidden
from the SDK docs and raises on call.

`attach.py` is a pure turn-boundary packer. It re-stats registered files,
reports rejected files in a leading text caption, and emits native image,
document, or media blocks after applying capability, count, size, and image
dimension limits. `ATTACH_MEDIA_MIME` is its shared suffix-to-MIME table for
both packing and `ava._understand`. Modality rejection happens at registration
(`attach()`), so the packer's caption-skip is only the safety net for entries
registered before a model change.
