---
name: media
description: "Generate an image (ChatGPT or Gemini) or video (Gemini) over the logged-in browser and download the resulting file — no API credits. Use when the user asks for a generated image, picture, or video. Videos are slow: submit, then poll back later."
---

# media — generate images and videos through the logged-in browser

Read [`../SKILL.md`](../SKILL.md) first for the shared model. This child sends an
image- or video-generation prompt to ChatGPT / Gemini, waits for the asset to
render, and downloads it to `~/Downloads/ava_<cluster>_web-ai/media/<stamp>-<site>-<slug>/`.
It spends no API credits — it uses the user's flat-rate web subscriptions.

## Images (fast — one call)

```bash
# ChatGPT or Gemini. Returns {state:"done", path, src, dir}.
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/media/reference/generate.py image \
    --site gemini --prompt "a photorealistic red panda on a skateboard, golden hour"

# Send the prompt exactly as written (no "Generate an image:" prefix):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/media/reference/generate.py image --site chatgpt --raw --prompt "..."
```

Images usually render in ~10-60s, so `image` fetches in one call. If it returns
`{state:"pending"}` the asset hadn't appeared within `--timeout` (default 120s)
— re-run `check --kind image --url <url>`.

## Videos (slow — submit, then poll)

Video generation (Gemini) can take from under a minute to several minutes;
don't block a turn on it:

```bash
# Submit; returns {state:"submitted", url}. Remember the url.
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/media/reference/generate.py video --site gemini --prompt "..."
```

Then poll like deep-research — schedule yourself with `ava.watcher.at(+Nmin)`,
idle, and on wake run `check`:

```bash
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/media/reference/generate.py check --site gemini --kind video --url "<url>"
# {state:"running"} -> reschedule + idle ; {state:"done", path|note} -> deliver
```

## Tabs

Each call drives its own browser tab. A finished result (`state:"done"`) closes
that tab so tabs don't pile up in the shared browser; a `pending` / `submitted`
result keeps it (the asset is still rendering — poll via `check --url`). Pass
`--keep-tab` to keep a done result's tab open. The `url` in every result is the
durable handle to revisit the conversation.

## What you get

The result JSON (also printed to stdout) always carries the asset's `src` URL,
so even if the download step fails you still have the link to hand the user.
Image downloads cover `https://`, `blob:` (re-encoded from the rendered image via
a canvas), and `data:` sources. **Video blobs (MediaSource) can't be fetched
out-of-page** — for those, `state` is `done` with `path: null` and a note; deliver
the video via the site's own download button (or hand the user the conversation
URL).

## Troubleshooting

- `pending` / `running` that never resolves → the asset selector may have drifted
  (the result is detected as the last large `<img>` / last `<video>`); open the
  URL in the browser, confirm it rendered, and if needed adjust `--min-px` or the
  finder in `generate.py`.
- `composer not found` / sign-in wall → that site isn't logged in; log in once.
- ChatGPT stalls → a Cloudflare bot-check may be up; clear it once in the browser.
