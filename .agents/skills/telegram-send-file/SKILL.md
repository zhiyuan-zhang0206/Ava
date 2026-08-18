---
name: telegram-send-file
description: Send a local file to the user's Telegram chat through the Bot API sendDocument endpoint. Use when the user asks to receive a file, report, or attachment.
---

# telegram-send-file

Send one local file to the user's Telegram private chat as a Telegram
document. This is the **only** direct Telegram Bot API call an agent may
make (user ruling 2026-08-13): the skill exists because IM Bridge has no
file channel. Everything else stays forbidden — reading updates, sending
text messages, or any other Bot API method. Text delivery always goes
through IM Bridge.

## When to use

The user asks for a file, report, image, archive, or other artifact to be
delivered to their Telegram chat. The file already exists on disk — this
skill does not generate content, it delivers it.

## Usage

Run from the Ava source root with the venv Python (never bare `python3`,
never `uv run`):

```bash
# From a dev checkout:
.venv/bin/python .agents/skills/telegram-send-file/scripts/send_file.py /path/to/file.pdf

# From the prod install:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/telegram-send-file/scripts/send_file.py /path/to/file.pdf

# With an optional caption (plain text, up to 1024 chars):
.venv/bin/python .agents/skills/telegram-send-file/scripts/send_file.py /path/to/file.zip --caption "Weekly report"
```

The script resolves the source root itself (the checkout it runs from, or
`$AVA_HOME/source`), so the invocation path does not matter — only the venv
Python matters: it must be the checkout's `.venv` so `httpx` and `shared`
are importable.

Output on success (stdout, one line):

```
sent report.pdf (12345 bytes) to chat 123456789: message_id=42
```

On failure the script prints `error: <reason>` to stderr and exits non-zero.

## What it does

1. **Validate** the path: exists, is a regular file, non-empty, at most
   50 MB (Telegram's Bot API document cap).
2. **Read credentials** from the cluster's env aliases — the same source
   IM Bridge uses: `AVA_TELEGRAM_BOT_TOKEN` and `AVA_TELEGRAM_OWNER_ID`
   (populated into the process env from `$AVA_HOME/.env` by `load_ava_env`;
   the script falls back to `settings.telegram.*` only in contexts that may
   construct that domain, e.g. bare CLI runs — agent processes cannot, per
   the per-process config matrix, Task #856). Never hardcode a token into a
   script, a skill, or a PR; never copy the token into a chat message, log
   line, or report.
3. **Send**: multipart `POST /bot<token>/sendDocument` with the file as the
   `document` part, the owner chat as `chat_id`, and the optional caption.
4. **Report**: prints the resulting `message_id` and the delivered size.

## Rules

- **One file per run.** Sending several files means running the script once
  per file (Telegram documents are single-file; there is no batch endpoint
  worth wrapping).
- **Sending is an outward-facing action.** When the file is not something
  the user already asked for, confirm the send with the user (or your
  delegator) first. The recipient is always the configured owner chat.
- **Token hygiene is absolute.** httpx error text can embed the request URL,
  which carries the bot token — errors are sanitized before they reach you;
  never print the token yourself, and never include it in a follow-up
  message or report.
- **No text messages through the Bot API.** If the user needs text alongside
  the file, deliver the text through IM Bridge or `ava.ui.notify` — only the
  `sendDocument` call is allowed here.
- **Caption is plain text.** No HTML/markdown rendering is attempted; a
  caption longer than 1024 chars is rejected before sending.
