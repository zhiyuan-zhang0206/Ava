---
name: sms
description: Read SMS/iMessage verification codes and recent messages from Messages.app on macOS. Use when an agent needs a 2FA code sent to the user's phone.
---

# sms

Read SMS/iMessage 2FA verification codes from macOS Messages.app. Agents run
the script when blocked by a 2FA prompt — no more interrupting the user to ask for codes.

**macOS only.** Requires Full Disk Access for the calling process (System Settings →
Privacy & Security → Full Disk Access).

## Usage

Run the self-contained script from the repo source root with the venv Python
(`.venv/bin/python` — never bare `python3`, never `uv run`):

```bash
# Get recent verification codes (any sender):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/sms/scripts/query.py --recent-codes

# Get codes from a specific phone number (last 4 digits):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/sms/scripts/query.py --recent-codes --phone-suffix 1118

# Get recent SMS messages:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/sms/scripts/query.py --recent-messages --limit 10

# Look back further:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/sms/scripts/query.py --recent-codes --lookback-hours 24
```

Output is plain text — one code/message per line, newest first.

## How it works

Reads `~/Library/Messages/chat.db` (SQLite) directly. When TCC blocks the read the
`sqlite3.OperationalError` propagates to the caller (fail fast — no fallback): grant
Full Disk Access to the calling process.

## Patterns detected

2FA codes matching: Chinese (verification code/check code), Korean (인증번호), Japanese (認証コード),
English (verification code / security code / OTP), Google-style (G-XXXXXX), and
standalone 4-8 digit sequences.
