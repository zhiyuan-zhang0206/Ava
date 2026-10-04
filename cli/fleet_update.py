"""Update a networked source-mode cluster over SSH, in two attended, idempotent halves.

`down --new SHA`: preflight; check the gateway's stored schedule scripts against NEW's code (a red
refuses, `--allow-red-schedules` overrides); stop each runner, then the gateway; switch every
checkout to NEW. `up`: start the gateway, then each runner (macOS as a one-time
GUI-session LaunchAgent: helper signing needs the login keychain); check holds
and the listed machines' roster rows; check for drift (the gateway's in-store schedule scripts, every
host's plugins); smoke-test each listed agent-runner; refresh skills.
After a failure, fix the cause and rerun the whole half. See conventions/runbook.md#updating-a-networked-cluster-in-source-mode.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from subprocess import PIPE, STDOUT
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
_HOME = 'H="$HOME/.ava"; S="$H/source"; cd "$S" || exit 1'
_AVA = 'AVA_HOME="$H" "$S/.venv/bin/ava"'
_PYTHON = f'{_HOME}; AVA_HOME="$H" AVA_CONFIG_FETCH=skip "$S/.venv/bin/python"'
_STATUS = f"{_HOME}; {_AVA} maintenance status"
_FREE = ("inactive", "resumed")  # maintenance statuses with no hold
_POLL_S = 5  # roster re-read interval
_STOP_HINT = (
    '\nOn that host retry "ava stop -y --timeout 600", confirm maintenance status is '
    "paused/stopped, then rerun `down`."
)
# Reports a dirty tree, never cleans it: untracked files are the operator's to move.
_CLEAN = (
    'd=$(git status --porcelain | head -n 20); [ -z "$d" ] || { echo "$d"; echo "Untracked files '
    "are not caused by this update (a changed .gitignore can reveal them): check them, then "
    'move them away (do not delete) and rerun down."; exit 1; }'
)
# `printf '%s\n'`, not `echo`: zsh's (and dash's) echo interprets backslashes, which corrupts the
# hold JSON when an argv in the record holds one (`\\$HOME` becomes `\$HOME`: an invalid escape).
_PROBE = f"""{_HOME}
printf '%s\\n' "head=$(git rev-parse HEAD)"
printf '%s\\n' "dirty=$(git status --porcelain 2>&1 | wc -l | tr -d ' ')"
test -e "$(git rev-parse --git-path hooks/post-checkout)" && echo hook=yes || echo hook=no
test -e "$H/updates/active" && echo active=yes || echo active=no
printf '%s\\n' "hold=$({_AVA} maintenance status 2>/dev/null | grep '^{{' | tail -n 1)\""""
# Early converge passes left these 0555; `uv sync` cannot write through them.
_VENV_DIRS = (
    "find .venv/bin .venv/lib -maxdepth 3 -type d \\( -path .venv/bin "
    '-o -path ".venv/lib/python*/site-packages" '
    '-o -path ".venv/lib/python*/site-packages/ava-*.dist-info" \\) -exec chmod u+w {} +'
)
_LOCKS = ("uv.lock", "ui/web/package-lock.json", ".python-version")
_WATCHED = ("migrations/", "services/permissions_helper/helper", *_LOCKS)
_SECRET_NAME = r"\w*(?:secret|password|token|api_key|capability_key)\w*"  # noqa: S105
_SECRETS = (
    (re.compile(r"(?i)(\bbearer\s+)[^\s\"',;]+"), r"\1<redacted>"),
    (re.compile(rf"(?i)(\b{_SECRET_NAME}[\"']?\s*[=:]\s*[\"']?)[^\s\"',;}}]+"), r"\1<redacted>"),
    (re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^/\s:@]*:)[^@\s/]+@"), r"\1<redacted>@"),
)
_GATEWAY_PROGRAM = """\
import json, sys, time
from base.cluster.machine import gateway_api_base, gateway_auth_headers
from base.host.net.http_dial import get, post
base, headers = gateway_api_base(), gateway_auth_headers()
if sys.argv[1] == "roster":
    rows = get(f"{base}/api/cluster/roster?fresh=true", headers=headers, timeout=90); rows.raise_for_status()
    keep = ("name", "online", "identity_mismatch", "head_sha", "running_sha", "serve_agent_runner")
    print(json.dumps([{key: row[key] for key in keep} for row in rows.json()])); sys.exit(0)
spawned = post(f"{base}/api/agents", headers=headers, json={"machine": sys.argv[2]}, timeout=60)
out = {"completed": False, "spawn": spawned.status_code}
if spawned.status_code >= 300:
    print(json.dumps(out)); sys.exit(1)
agent = out["agent"] = spawned.json()["id"]
try:
    message = {"content": "Update smoke test. Run exactly this Python code with your code tool, once: print(1 + 2)", "source": "user"}
    post(f"{base}/api/agents/{agent}/messages", headers=headers, json=message, timeout=60).raise_for_status()
    deadline = time.monotonic() + float(sys.argv[3])
    while not out["completed"] and time.monotonic() < deadline:
        time.sleep(4)
        items = get(f"{base}/api/agents/{agent}/timeline", headers=headers, timeout=60).json()["items"]
        code = [i for i, item in enumerate(items) if item["kind"] == "agent_code" and "print(1+2)" in item["payload"].replace(" ", "")]
        outputs = [item["payload"] for item in items[code[-1] + 1:] if item["kind"] == "code_output"] if code else []
        out["completed"] = bool(outputs) and outputs[0].partition("\\n\\n")[2].strip().startswith("3")
finally:
    out["terminate"] = post(f"{base}/api/agents/{agent}/terminate", headers=headers, timeout=60).status_code
    print(json.dumps(out))
sys.exit(0 if out["completed"] and out["terminate"] < 300 else 2)
"""


# Before anything stops: NEW's `ava schedules verify` over the running gateway's schedule table
# (every stored script, agent-written ones included). The table is read by the home's own source
# checkout (OLD), the only code the database authority admits; NEW's source, from a throwaway
# worktree, receives the rows as a file and checks them offline on the host's current interpreter
# (cwd wins sys.path, so NEW's modules are the ones imported). Nothing dials the database from the
# worktree and no service is touched. `VERIFY_RC` is the verdict, so a crashed check (unreadable
# table, new dependency missing) is told apart from a red.
_PRE_VERIFY = """{home}
W="$H/pre-update-verify"; R="$W.rows"
trap 'git -C "$S" worktree remove --force "$W" >/dev/null 2>&1; rm -f "$R"' EXIT
git -C "$S" worktree remove --force "$W" >/dev/null 2>&1; rm -rf "$W"; git -C "$S" worktree prune
git cat-file -e "{new}^{{commit}}" || {{ echo "SKIPPED: {new} is not fetched on this host (a dry run does not fetch)"; exit 0; }}
(umask 077; AVA_HOME="$H" AVA_CONFIG_FETCH=skip "$S/.venv/bin/python" -c \\
  'import json, sys; from cli.commands.management.schedules_verify import _read_schedule_rows as rows; json.dump(rows(), open(sys.argv[1], "w"))' "$R") \\
  || {{ echo "VERIFY_RC=2"; exit 0; }}
git -C "$S" worktree add -q "$W" {new} || {{ echo "VERIFY_RC=2"; exit 0; }}
cd "$W" && AVA_HOME="$H" AVA_CONFIG_FETCH=skip "$S/.venv/bin/python" -c \\
  'import sys; from cli.commands.management.schedules_verify import cmd_schedules_verify as v; sys.exit(v(notify=False, rows_file=sys.argv[1]))' "$R"
echo "VERIFY_RC=$?"; exit 0"""


class FailedError(RuntimeError):
    """A step failed; fix it, then rerun the whole half."""


def ssh(alias: str, command: str, stdin: str | None, emit: Callable[[str], None]) -> int:
    """Run `command` on `alias` through its account's shell, emitting each output line."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", alias, command]
    pipe = PIPE if stdin is not None else subprocess.DEVNULL
    process = subprocess.Popen(argv, stdin=pipe, stdout=PIPE, stderr=STDOUT, text=True)  # noqa: S603
    if stdin is not None and process.stdin is not None:
        process.stdin.write(stdin)
        process.stdin.close()
    for line in process.stdout or ():
        emit(line.rstrip("\n"))
    return process.wait()


def _hold(text: str) -> tuple[str, str | None, dict[str, str]]:
    data = json.loads([line for line in text.splitlines() if line.startswith("{")][-1])
    held = data["maintenance"]
    return (data["status"], held["phase"], held["failures"]) if held else (data["status"], None, {})


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(_REPO), *args]
    return subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603


class Session:
    """One half: the dry-run switch, each host's OS, and the tee'd, redacted log."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.dry_run: bool = args.dry_run
        args.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = args.log_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S.%fZ}-{args.half}.log"
        self.log = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        self.say(f"log: {path}")
        self.os: dict[str, str] = {}  # alias -> `uname -s`; commands run raw until it is known

    def say(self, line: str) -> None:
        for pattern, replacement in _SECRETS:
            line = pattern.sub(replacement, line)
        print(line, flush=True)
        self.log.write(line + "\n")
        self.log.flush()

    def run(self, alias: str, script: str, *, effect: bool = True, stdin: str | None = None) -> str:
        """`script` on `alias` (in its login shell once the OS is known); FailedError on exit != 0."""
        self.say(f"[{alias}] {'$' if effect else 'read:'} " + script.replace("\n", "\n    "))
        if effect and self.dry_run:
            return ""
        if alias in self.os:
            script = f"{'zsh' if self.os[alias] == 'Darwin' else 'bash'} -lc {shlex.quote(script)}"
        lines: list[str] = []

        def emit(line: str) -> None:
            lines.append(line)
            self.say(f"    {alias}| {line}")

        if rc := ssh(alias, script, stdin, emit):
            raise FailedError(f"{alias}: exit {rc}: {lines[-1] if lines else 'no output'}")
        return "\n".join(lines)


def _host_refusals(alias: str, facts: dict[str, str]) -> list[str]:
    hold = facts["hold"] or '{"status": "unreadable", "maintenance": null}'
    status, phase, failures = _hold(hold)
    reasons: list[str] = []
    if facts["dirty"] != "0":
        reasons.append(f"{alias}: $HOME/.ava/source has uncommitted changes")
    if facts["hook"] == "yes":
        reasons.append(f"{alias}: a post-checkout hook (legacy guard recurses); move it aside")
    if facts["active"] == "yes":
        reasons.append(f"{alias}: $HOME/.ava/updates/active names an unfinished release operation")
    # A completed, failure-free stop is what an interrupted `down` leaves; rerunning is fine.
    stopped = (status, phase) == ("paused", "stopped") and not failures
    if status not in _FREE and not stopped:
        reasons.append(f"{alias}: maintenance hold {status}/{phase} failures={failures}")
    return reasons


def _preflight(s: Session, args: argparse.Namespace, new: str) -> list[str]:
    """The four refusals, HEAD agreement, and the OLD..NEW report (OLD = the unswitched HEAD)."""
    reasons: list[str] = []
    heads: dict[str, str] = {}
    for alias in [args.gateway, *args.runner]:
        probe = s.run(alias, _PROBE, effect=False).splitlines()
        facts = {key: value for key, _, value in (line.partition("=") for line in probe)}
        heads[alias] = facts["head"]
        reasons += _host_refusals(alias, facts)
    old = {head for head in heads.values() if head != new}
    if len(old) > 1:
        return [*reasons, f"hosts disagree on HEAD: {heads}"]
    if old and _git("cat-file", "-e", f"{next(iter(old))}^{{commit}}").returncode:
        return [*reasons, f"OLD {next(iter(old))} is not in {_REPO}: git fetch origin"]
    if old:
        stat = _git("diff", "--stat", next(iter(old)), new, "--", *_WATCHED).stdout.rstrip()
        s.say(stat or "no change under " + " ".join(_WATCHED))
        python = _git("diff", "--quiet", next(iter(old)), new, "--", ".python-version")
        if python.returncode and not args.allow_python_change:
            reasons.append(".python-version changes (venvs rebuild): pass --allow-python-change")
    return reasons


def _stored_schedules(s: Session, args: argparse.Namespace, new: str) -> None:
    """Refuse to stop a cluster whose stored schedule scripts NEW's code cannot run.

    A schedule script lives in the database, not the checkout: a library change (a moved module, a
    changed signature) leaves it crash-looping the moment `up` starts the schedule-manager, and the
    gateway's own `ava schedules verify` only exists after that start. Run NEW's verify first."""
    out = s.run(args.gateway, _PRE_VERIFY.format(home=_HOME, new=new), effect=False)
    lines = out.splitlines()
    if any(line.startswith("SKIPPED") for line in lines):
        s.say("stored schedule scripts: not checked (see above)")
        return
    code = next((line.partition("=")[2] for line in lines if line.startswith("VERIFY_RC=")), "?")
    if code == "0":
        return
    reds = [line for line in lines if line.startswith(("RED ", "TOOL-ERROR"))]
    what = "red" if code == "1" else f"unevaluable (check exited {code})"
    detail = "\n  ".join(reds) or "no RED line: see the output above"
    if args.allow_red_schedules:
        s.say(f"WARNING: stored schedule scripts {what} under {new[:12]}, continuing:\n  {detail}")
        return
    raise FailedError(
        f"refused: stored schedule scripts are {what} under {new[:12]} and would crash-loop after "
        f"`up`:\n  {detail}\nFix them (`ava schedules update <id> --script-file ...` on the gateway) "
        "and rerun `down`, or pass --allow-red-schedules to proceed anyway."
    )


def down(s: Session, args: argparse.Namespace) -> None:
    new = _git("rev-parse", "--verify", f"{args.new}^{{commit}}").stdout.strip()
    if not new:
        raise FailedError(f"refused: {args.new} is not a commit in {_REPO}; git fetch origin")
    if reasons := _preflight(s, args, new):
        raise FailedError("refused: " + "; ".join(reasons))
    everyone = [args.gateway, *args.runner]
    for alias in everyone:  # before any stop: a missing NEW must not strand a stopped cluster
        s.run(alias, f'{_HOME}; git cat-file -e "{new}^{{commit}}" || git fetch -q origin {new}')
    _stored_schedules(s, args, new)
    for alias in [*args.runner, args.gateway]:
        try:
            s.run(alias, f"{_HOME}; {_AVA} stop -y --timeout {args.stop_timeout}")
            if not s.dry_run:
                status, phase, failures = _hold(s.run(alias, _STATUS, effect=False))
                if (status, phase) != ("paused", "stopped") or failures:
                    raise FailedError(f"{alias}: stop left {status}/{phase} failures={failures}")  # noqa: TRY301
        except FailedError as exc:
            raise FailedError(f"{exc}{_STOP_HINT}") from exc
    for alias in everyone:
        s.run(
            alias,
            f"{_HOME}; git checkout -q --detach {new} && {_VENV_DIRS} && "
            f"env -u VIRTUAL_ENV uv sync --frozen --compile-bytecode && {_CLEAN}",
        )
    s.say(f"down complete: every host is stopped on {new[:12]}; run `up` when ready")


def gui_oneshot(alias: str, timeout_s: int) -> str:
    """`ava start` as a run-once gui/<uid> LaunchAgent; left loaded (not killed) past the deadline."""
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    label = f"com.ava.fleet-update.{re.sub(r'[^A-Za-z0-9.-]', '-', alias)}.{stamp}"
    log = f"/tmp/{label}/oneshot.log"  # noqa: S108 — remote path in a fresh 0700 directory
    start = f"{_HOME}; {_AVA} start"
    job = {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", "-lc", f'({start}); echo "ONESHOT_RC=$?"'],
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return f"""D=/tmp/{label}; mkdir -m 700 "$D" && : > "{log}" || exit 70
cat > "$D/job.plist" <<'AVA_FLEET_PLIST'
{plistlib.dumps(job).decode().rstrip()}
AVA_FLEET_PLIST
launchctl bootstrap "gui/$(id -u)" "$D/job.plist" || {{ echo "not logged in to the GUI?"; exit 71; }}
n=0; until grep -q '^ONESHOT_RC=' "{log}"; do
  [ "$n" -ge {timeout_s // 5} ] && {{ echo "still running: gui/$(id -u)/{label}, {log}"; exit 72; }}
  sleep 5; n=$((n+1)); done
cat "{log}"; rc=$(sed -n 's/^ONESHOT_RC=//p' "{log}" | tail -n 1)
launchctl bootout "gui/$(id -u)/{label}" || echo "bootout of {label} failed"
rm -rf "$D"; exit "$rc\""""


def _machine_names(s: Session, aliases: list[str]) -> dict[str, str]:
    """Each listed host's own machine name (what the roster calls it), asked of the host.

    Asked with `-c`, so it takes `_PYTHON`, not the `python -` program that reads stdin."""
    ask = f'{_PYTHON} -c "from base.cluster.machine import machine_name; print(machine_name())"'
    names: dict[str, str] = {}
    for alias in aliases:
        reported = s.run(alias, ask, effect=False).split()
        if not reported:
            raise FailedError(f"{alias}: the host printed no machine name")
        names[alias] = reported[-1]
    if len(set(names.values())) != len(names):
        raise FailedError(f"two listed hosts report one machine name: {names}")
    return names


def _roster(s: Session, args: argparse.Namespace, program: str) -> tuple[list[dict[str, Any]], str]:
    """The listed machines' roster rows and the one commit they all run, online, at that checkout.

    Only the `--gateway`/`--runner` machines are required; the rest of the roster (a laptop
    that is off) is reported and left alone. A listed host is matched to its row by the
    machine name it reports, not by its SSH alias; no row for it is an error.
    `online` follows the heartbeat: a machine just started is re-read until --roster-timeout."""
    names = _machine_names(s, [args.gateway, *args.runner])
    wanted = set(names.values())
    deadline = time.monotonic() + args.roster_timeout
    while True:
        out = s.run(args.gateway, f"{program} roster", effect=False, stdin=_GATEWAY_PROGRAM)
        rows = json.loads(out.splitlines()[-1])
        if missing := sorted(wanted - {row["name"] for row in rows}):
            raise FailedError(f"no roster row for {missing} (host aliases to names: {names})")
        listed = [row for row in rows if row["name"] in wanted]
        offline = [row["name"] for row in listed if not row["online"] or row["identity_mismatch"]]
        commits = {(row["head_sha"], row["running_sha"]) for row in listed}
        commit = listed[0]["head_sha"]
        if not offline and commit is not None and commits == {(commit, commit)}:
            _report_unlisted(s, rows, wanted)
            return listed, commit
        if time.monotonic() >= deadline:
            _report_unlisted(s, rows, wanted)
            raise FailedError(
                f"roster after {args.roster_timeout}s: offline {offline}; commits {commits}"
            )
        time.sleep(_POLL_S)


def _report_unlisted(s: Session, rows: list[dict[str, Any]], wanted: set[str]) -> None:
    for row in rows:
        if row["name"] not in wanted:
            s.say(
                f"roster: {row['name']} is not listed, not checked: online={row['online']} "
                f"head={row['head_sha']} running={row['running_sha']}"
            )


def _refresh(s: Session, args: argparse.Namespace) -> None:
    """Skills follow their channel, not the checkout; a differing local copy is replaced."""
    for alias in [args.gateway, *args.runner]:
        out = s.run(alias, f"{_HOME}; {_AVA} packages refresh")
        if out:  # empty on a dry run
            s.say(f"packages refresh [{alias}] {out.splitlines()[-1].strip()}")


def _drift_checks(s: Session, args: argparse.Namespace) -> None:
    """Library changes that stale what lives outside the checkout, found before an agent runs on it.

    `ava schedules verify` on the gateway (the in-store schedule scripts: imports resolve and every
    call into repo code still binds) and `ava plugins verify` on every host (each enabled plugin
    loads; the agent loader contains a broken one, so only this surfaces it). Both read-only.
    Every check runs; each one's detail is in the log above, and any red fails `up`."""
    checks = [(args.gateway, "schedules verify --no-notify")]
    checks += [(alias, "plugins verify") for alias in [args.gateway, *args.runner]]
    failed: list[str] = []
    for alias, verb in checks:
        try:
            # effect=True so a dry run lists the check without running it.
            s.run(alias, f"{_HOME}; {_AVA} {verb}", effect=True)
        except FailedError as exc:
            failed.append(f"`ava {verb}` {exc}")
    if failed:
        raise FailedError("drift check failed: " + "; ".join(failed))


def up(s: Session, args: argparse.Namespace) -> None:
    for alias in [args.gateway, *args.runner]:
        macos = s.os[alias] == "Darwin"
        s.run(alias, gui_oneshot(alias, args.start_timeout) if macos else f"{_HOME}; {_AVA} start")
        if not s.dry_run and (hold := _hold(s.run(alias, _STATUS, effect=False)))[0] not in _FREE:
            raise FailedError(f"{alias}: start left maintenance {hold}")
    program = f"{_PYTHON} -"
    if s.dry_run:
        _drift_checks(s, args)
        s.run(args.gateway, f"{program} smoke MACHINE {args.smoke_timeout}  # each agent-runner")
        _refresh(s, args)
        return
    rows, commit = _roster(s, args, program)
    _drift_checks(s, args)
    for name in [row["name"] for row in rows if row["serve_agent_runner"]]:
        smoke = f"{program} smoke {shlex.quote(name)} {args.smoke_timeout}"
        s.run(args.gateway, smoke, stdin=_GATEWAY_PROGRAM)
    _refresh(s, args)
    s.say(f"up complete: every listed machine runs {commit[:12]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cli.fleet_update", description=__doc__)
    halves = parser.add_subparsers(dest="half", required=True)
    for half in ("down", "up"):
        p = halves.add_parser(half)
        p.add_argument("--gateway", required=True, metavar="SSH_ALIAS")
        p.add_argument("--runner", action="append", default=[], metavar="SSH_ALIAS")
        p.add_argument("--log-dir", type=Path, required=True)
        p.add_argument("--dry-run", action="store_true", help="read-only preflight, print effects")
        if half == "down":
            p.add_argument("--new", required=True, help="commit to switch every host to")
            p.add_argument("--allow-python-change", action="store_true")
            p.add_argument("--stop-timeout", type=int, default=600)
            p.add_argument(
                "--allow-red-schedules",
                action="store_true",
                help="proceed although stored schedule scripts fail NEW's `ava schedules verify`",
            )
        else:
            p.add_argument("--start-timeout", type=int, default=1800)
            p.add_argument("--smoke-timeout", type=int, default=300)
            p.add_argument("--roster-timeout", type=int, default=90)
    args = parser.parse_args(argv)
    if "AVA_AGENT_ID" in os.environ:
        print("refused: run from a plain login shell, not an Ava agent's shell (a stop closes it)")
        return 2
    s = Session(args)
    try:
        hosts = [args.gateway, *args.runner]
        s.os = {host: s.run(host, "uname -s", effect=False).split()[-1] for host in hosts}
        (down if args.half == "down" else up)(s, args)
    except FailedError as exc:
        s.say(f"FAILED: {exc}\nNothing rolled back. Fix it, then rerun `{args.half}` (idempotent).")
        return 2 if str(exc).startswith("refused") else 1
    finally:
        s.log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
