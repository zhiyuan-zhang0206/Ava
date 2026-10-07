# The CLI refuses every command from a checkout that is not the home's own

## Context

A home that carries its own `<home>/source` checkout (production, and every unit
started from source) is operated only by that checkout's code
(`home_checkout_error`). The CLI gate in front of `ava` enforced that for the verbs
that change state and exempted the ones that only read, through an allowlist of 30
verb paths (`status`, `agents ls`, `config get`, ...), a helper that cut the argv into
a verb path, and a separate check in `cli.main` that skipped the gate whenever `-h` or
`--help` appeared anywhere in argv.

The list had to be kept in step with the parser by hand, and the skip was wider than
what argparse does with the flag. Two invocations from a foreign checkout got past
the gate: `ava config unset -- --help` (after `--` the token is a positional the verb
takes as data, so the handler ran) and `ava boot --help` (`boot` is dispatched before
the parser, so the flag never reaches argparse; it opened the home's boot log).

## Decision

The gate keeps protection only where the rule is certain to be broken and the
consequence cannot be undone: a disposable checkout driving a long-lived home binds
its daemons to code that disappears with the checkout, or applies unreviewed
migrations. There, it refuses every command, with one pass: an argv of nothing, or of
exactly one `-h` or `--help`, which only parses and reaches no verb.

The refusal names the two ways out: the home's own CLI (`<home>/source/.venv/bin/ava`,
the bare `ava` of a production host), or a temporary `AVA_HOME` for a development CLI.
Reading production from a development checkout is done with the bare `ava`, never
with the checkout's CLI. The refusal does not echo the argv, which can carry a secret.

## Alternatives rejected

- **Keep the allowlist and fix its edges** (scan for the help flag only before `--`,
  special-case `boot`). It leaves a list of verb paths that every new verb must be
  classified against, and an argv scanner that has to stay as exact as argparse.
- **Exempt `<verb> --help`.** It is safe only through that argv scanner. A developer
  who wants the help of the CLI under development names a temporary `AVA_HOME`, which
  every test and tool already does.

## Consequences

- No list to keep: a verb added later is refused without anyone having to decide that
  it changes nothing.
- A development checkout can no longer run `ava status` against the host's own home;
  `status` and the rest of the read verbs are one `AVA_HOME=<temporary directory>` or
  one bare `ava` away.
- Homes without a `source` (tests, scratch homes) accept every command, as before.
  `cli.fleet_update` drives each host through the home's own `$S/.venv/bin/ava`, so it
  is never refused.
