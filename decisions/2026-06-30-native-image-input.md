# Native image input, capability-gated

## Context

The message pipeline was string end-to-end: `inbound_messages.content` is `TEXT`,
the claim node envelope-wraps that string into a `HumanMessage(content=str)`, and
the frontend composer sent `{content: string}`. Images could only reach an agent
indirectly — the upload endpoint saved a file to `~/Downloads/AvaAgent-{id}/` and
delivered a text notification ("File uploaded: /path"), leaving the agent to call
`ava.understand(path=...)` (which routes media to Gemini) to actually see it. We
wanted a user to paste/drag an image into chat and have the agent's own model see
the pixels natively.

The complication: the default agent model is `deepseek-v4-pro`, invoked over
DeepSeek's Anthropic-compatible endpoint, which does not decode image content
blocks. Only `claude-*` / `gemini-*` / `gpt-*` bindings are multimodal. So "native
image input" cannot be universal — it depends on the agent's model.

## Decision

Thread `str | list[ContentBlock]` (OpenAI-shaped `text` / `image_url` blocks)
through the message endpoint, and **gate on model capability**: if an image is
addressed to an agent whose effective model is not vision-capable, the send
endpoint returns **422** up front with a clear message. Storage is
reference-based, not inline: the image is uploaded to disk (reusing the existing
upload dir) and only its reference url travels on the wire and in the
inbound row's JSONB `payload`. The claim node reads the file at delivery time and
inlines it as a native base64 image block in the `HumanMessage`; the reference
url rides on `additional_kwargs.ava_image_urls` so the timeline renders a
thumbnail without ever surfacing the base64.

## Alternatives rejected

- **Understand-mediated (model-agnostic).** Keep content as a string; on image
  send, attach the file path and let the agent call `ava.understand`. Works with
  every model including DeepSeek, tiny change — but the model never sees pixels,
  only a description it must actively fetch. Rejected: it is not "native", and it
  is essentially the flow that already existed.
- **Silently drop / auto-fall-back to a text description for non-vision models.**
  Rejected as a compatibility shim: it hides the real constraint. Fail-fast 422 at
  the boundary tells the user exactly why (switch to a vision model) instead of
  producing a degraded answer that looks like the model saw the image.
- **Base64 inline in the message / DB.** Sending base64 in the POST body blows the
  content size limit; storing base64 in `inbound_messages.content` bloats the row
  and would be mangled by the envelope wrap. Reference-by-url keeps the wire and
  the DB small.
- **Serve the image to the provider by URL** (Anthropic `url` image source).
  Rejected: a local / split-deployment gateway is not reachable by the provider's
  servers, and DeepSeek's endpoint will not fetch arbitrary urls. Base64,
  materialized locally at claim time, is the only form that works offline.

## Consequences

- The feature is dark on the prod default model (`deepseek-v4-pro`): image
  messages 422 until the agent is switched to a vision-capable model. This is
  intended and surfaced, not a bug.
- Base64 is materialized into `state.messages` at claim time, so a checkpoint
  carries it thereafter (the cost of keeping an image in context — inherent to any
  multimodal agent). It is captured once and never re-read, so later deletion of
  the on-disk file does not break the conversation.
- Claim-time base64 reads the upload from the agent-runner's **local** disk, the
  same co-location assumption the existing upload flow already makes. In a split
  gateway/runner deployment the file lives on the gateway, so an image degrades to
  a "[image unavailable]" text note rather than crashing the claim. The timeline
  `<img>` is served by the gateway (which has the file) and works either way.
- A `model_supports_vision(model)` predicate (`shared/lm/factory.py`) is the single
  gate; adding a provider's vision support is a one-line prefix edit there.
