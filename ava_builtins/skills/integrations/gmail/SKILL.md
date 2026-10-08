---
name: gmail
description: "Reads and manages Gmail through IMAP/SMTP. Use when accessing Gmail messages, drafting or sending mail, or ingesting Gmail newsletters."
---

# gmail

Full Gmail client, pure IMAP/SMTP, self-contained CLI. The agent invokes `scripts/feed.py`
via bash and reads JSON from stdout. **Do not import into agent namespace, do not drive a browser.**

11 lenses: `search` / `read` / `discover` / `enum` / `fetch` / `sync` / `send` /
`reply` / `forward` / `draft` / `draft-delete`.

Gmail's `category:`, `list:`, `after:` are passed through as-is via the `X-GM-RAW`
IMAP extension; the full Gmail search syntax is supported.

## One-time Setup

1. **Enable 2-Step Verification + generate App Password**: Google Account → Security →
   `myaccount.google.com/apppasswords` generate a 16-character App Password.
2. **Enable IMAP**: Gmail settings → Forwarding and POP/IMAP → Enable IMAP.
3. **Store the App Password** — two sources are read, in this order:
   - **Plain-text file (read first)**: `~/.ava/secrets/gmail-app-password`, `chmod 600` — the setup for headless machines that cannot reach the Keychain.
   - **Keychain entry (fallback)**:
     ```bash
     security add-generic-password -a <you@gmail.com> -s ava-gmail-imap -w
     ```
     The first Keychain access may pop up an 'Allow' dialog; click Always Allow.
   The login (account) is the `GMAIL_ACCOUNT` env var when set, else the Keychain entry's `acct` label.

## Usage

> **Environment**: scripts run from the load dir (`$AVA_HOME/skills/`) with the
> checkout's venv (`$AVA_HOME/source/.venv/bin/python`; in a dev checkout use
> `<checkout>/.venv/bin/python` instead). `$AVA_HOME` is set in agent processes.
```bash
# ── Reading side ──

# General search (full Gmail syntax), returns metadata:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py search --query "from:boss newer_than:7d" --limit 20

# Read email body (headers + extracted text):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py read --message-id "<abc@host>"
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py read --query "subject:invoice newer_than:30d"

# Fetch a single raw .eml + attachments to ~/Downloads/gmail/:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py fetch --message-id "<abc@host>"

# ── Newsletter ──

# Discover newsletters that appeared within the time window (grouped by List-Id):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py discover --since 60d

# Enumerate recent issues of a newsletter:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py enum --list-id newsletter.example.com --since 30d

# All-in-one: enumerate + fetch each:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py sync --list-id newsletter.example.com --since 30d

# ── Writing side (has side effects, default to --dry-run) ──

# Send new email:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py send --to a@b.com --subject "Hi" --body "..." --dry-run

# Reply (auto Re: prefix + In-Reply-To/References threading):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py reply --message-id "<abc@host>" --body "..." --dry-run
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py reply --message-id "<abc@host>" --body "..." --reply-all --dry-run

# Forward (auto Fwd: prefix + original message quote + auto-attach original attachments):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py forward --message-id "<abc@host>" --to a@b.com --dry-run

# Send with attachments (--attach can be repeated):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py send --to a@b.com --subject "Records" --body "see attached" --attach ~/Downloads/x.pdf --dry-run

# ── Drafts ──

# Save draft (does not send, goes to Drafts folder; can be edited and sent later from web/mobile Gmail):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py draft --to a@b.com --subject "Hi" --body "..."

# Delete draft (by message_id returned when saving):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/gmail/scripts/feed.py draft-delete --message-id "<abc@host>"
```

`--since` accepts `30d`, `12h`, or `2026-05-07`; `--limit N` limits number of items. Results are printed as JSON to stdout.

## Writing Side Precautions

`send`/`reply`/`forward` will **actually send emails**:

- **Always `--dry-run` first**: constructs the email and prints to stdout, does not connect to SMTP.
- **Confirm with user before sending** (recipients/subject/body) — this is an irreversible external action.
- `--body` gives the body text, or omit `--body` to read from stdin.
- `--to`/`--cc` separate multiple addresses with commas.
- From address is the `GMAIL_ACCOUNT` when set, else the Keychain entry's account.

## Troubleshooting

| Error | Cause / Solution |
|------|------------|
| `could not read Keychain entry` / `no Gmail app password` | No usable password source: create the file and/or the `ava-gmail-imap` entry; follow step 3 of the setup above |
| Headless agent, no Keychain access | Use the plain-text file at `~/.ava/secrets/gmail-app-password` (read first; machine-level, same as the Keychain entry — not per-cluster); set `GMAIL_ACCOUNT` when the Keychain cannot supply the account label |
| `IMAP login failed` | App Password expired (regenerate) or IMAP not enabled |
| `SMTP login failed` | Same as above; the same App Password is used for SMTP |
| `Gmail refused the search ...` | Used unsupported search operator; switch to syntax supported by X-GM-RAW |
| `discover` does not list a certain publication | It may be in the promotions category (by default only searches updates/forums) |
