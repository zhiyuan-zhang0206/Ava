# ava:ref cli/docs/fleet-update.ava.okf.md#no-application-imports

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from subprocess import PIPE, STDOUT
from typing import Any

REPO = Path(__file__).resolve().parents[1]
HOME = 'H="$HOME/.ava"; S="$H/source"; cd "$S" || exit 1'
AVA = 'AVA_HOME="$H" "$S/.venv/bin/ava"'
_PYTHON = f'{HOME}; AVA_HOME="$H" AVA_CONFIG_FETCH=skip "$S/.venv/bin/python"'
_STATUS = f"{HOME}; {AVA} status --json"
_FREE = ("inactive", "resumed")
POLL_S = 5
KEEP_TIMEOUT_S = 300.0
_STOP_HINT = (
    '\nOn that host retry "ava stop -y --timeout 600", confirm the maintenance hold is '
    "paused/stopped (`ava status`), then rerun `down`."
)
CLEAN = (
    'd=$(git status --porcelain | head -n 20); [ -z "$d" ] || { echo "$d"; echo "Untracked files '
    "are not caused by this update (a changed .gitignore can reveal them): check them, then "
    'move them away (do not delete) and rerun down."; exit 1; }'
)
PROBE = f"""{HOME}
printf '%s\\n' "head=$(git rev-parse HEAD)"
printf '%s\\n' "dirty=$(git status --porcelain 2>&1 | wc -l | tr -d ' ')"
test -e "$(git rev-parse --git-path hooks/post-checkout)" && echo hook=yes || echo hook=no
test -e "$H/updates/active" && echo active=yes || echo active=no
printf '%s\\n' "hold=$({AVA} status --json 2>/dev/null | grep '^{{' | tail -n 1)\""""
_VENV_DIRS = (
    "find .venv/bin .venv/lib -maxdepth 3 -type d \\( -path .venv/bin "
    '-o -path ".venv/lib/python*/site-packages" '
    '-o -path ".venv/lib/python*/site-packages/ava-*.dist-info" \\) -exec chmod u+w {} +'
)
_LOCKS = ("uv.lock", "ui/web/package-lock.json", ".python-version")
_WATCHED = ("migrations/", "services/desktop/permissions_helper/helper", *_LOCKS)
_SECRET_NAME = r"\w*(?:secret|password|token|api_key|capability_key)\w*"
_SECRETS = (
    (re.compile(r"(?i)(\bbearer\s+)[^\s\"',;]+"), r"\1<redacted>"),
    (re.compile(rf"(?i)(\b{_SECRET_NAME}[\"']?\s*[=:]\s*[\"']?)[^\s\"',;}}]+"), r"\1<redacted>"),
    (re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^/\s:@]*:)[^@\s/]+@"), r"\1<redacted>@"),
)


def gateway_program() -> str:
    return (Path(__file__).parent / "commands" / "management" / "fleet_gateway.py").read_text(
        encoding="utf-8"
    )


VERIFY_SCRIPTS = Path(__file__).parent / "commands" / "management"


def read_rows_py() -> str:
    return (VERIFY_SCRIPTS / "schedules_verify_read_rows.py").read_text(encoding="utf-8")


def check_rows_py() -> str:
    return (VERIFY_SCRIPTS / "schedules_verify_check_rows.py").read_text(encoding="utf-8")


PRE_VERIFY = """{home}
W="$H/pre-update-verify"; R="$W.rows"; T=""
trap 'git -C "$S" worktree remove --force "$W" >/dev/null 2>&1; rm -f "$R"; [ -z "$T" ] || rm -rf "$T"' EXIT
git -C "$S" worktree remove --force "$W" >/dev/null 2>&1; rm -rf "$W"; git -C "$S" worktree prune
git cat-file -e "{new}^{{commit}}" || {{ echo "SKIPPED: {new} is not fetched on this host (a dry run does not fetch)"; exit 0; }}
(umask 077; AVA_HOME="$H" AVA_CONFIG_FETCH=skip "$S/.venv/bin/python" -c {read_rows} "$R") \\
  || {{ echo "VERIFY_RC=2"; exit 0; }}
git -C "$S" worktree add -q "$W" {new} || {{ echo "VERIFY_RC=2"; exit 0; }}
T=$(mktemp -d) || {{ echo "VERIFY_RC=2"; exit 0; }}
cd "$W" && AVA_HOME="$T" AVA_CONFIG_FETCH=skip "$S/.venv/bin/python" -c {check_rows} "$R"
echo "VERIFY_RC=$?"; exit 0"""


class FailedError(RuntimeError):
    pass


def ssh(alias: str, command: str, stdin: str | None, emit: Callable[[str], None]) -> int:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", alias, command]
    pipe = PIPE if stdin is not None else subprocess.DEVNULL
    process = subprocess.Popen(argv, stdin=pipe, stdout=PIPE, stderr=STDOUT, text=True)
    if stdin is not None and process.stdin is not None:
        process.stdin.write(stdin)
        process.stdin.close()
    for line in process.stdout or ():
        emit(line.rstrip("\n"))
    return process.wait()


def split_hold_text(text: str) -> tuple[str, str | None]:
    data = json.loads([line for line in text.splitlines() if line.startswith("{")][-1])
    hold = data["hold"]
    held = hold["maintenance"]
    return (hold["status"], held["phase"]) if held else (hold["status"], None)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(REPO), *args]
    return subprocess.run(command, capture_output=True, text=True, check=False)


class Session:
    def __init__(self, args: argparse.Namespace) -> None:
        self.dry_run: bool = args.dry_run
        args.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = args.log_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S.%fZ}-{args.half}.log"
        self.log = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        self.say(f"log: {path}")
        self.os: dict[str, str] = {}

    def say(self, line: str) -> None:
        for pattern, replacement in _SECRETS:
            line = pattern.sub(replacement, line)
        print(line, flush=True)
        self.log.write(line + "\n")
        self.log.flush()

    def run(self, alias: str, script: str, *, effect: bool = True, stdin: str | None = None) -> str:
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
    hold = facts["hold"] or '{"hold": {"status": "unreadable", "maintenance": null}}'
    status, phase = split_hold_text(hold)
    reasons: list[str] = []
    if facts["dirty"] != "0":
        reasons.append(f"{alias}: $HOME/.ava/source has uncommitted changes")
    if facts["hook"] == "yes":
        reasons.append(f"{alias}: a post-checkout hook (the guard recurses); move it aside")
    if facts["active"] == "yes":
        reasons.append(f"{alias}: $HOME/.ava/updates/active names an unfinished release operation")
    stopped = (status, phase) == ("paused", "stopped")
    if status not in _FREE and not stopped:
        reasons.append(f"{alias}: maintenance hold {status}/{phase}")
    return reasons


# ava:ref cli/docs/fleet-update-window.ava.okf.md#preflight-refusals
def _preflight(s: Session, args: argparse.Namespace, new: str) -> tuple[list[str], str | None]:
    reasons: list[str] = []
    heads: dict[str, str] = {}
    for alias in [args.gateway, *args.runner]:
        probe = s.run(alias, PROBE, effect=False).splitlines()
        facts = {key: value for key, _, value in (line.partition("=") for line in probe)}
        heads[alias] = facts["head"]
        reasons += _host_refusals(alias, facts)
    old = {head for head in heads.values() if head != new}
    if len(old) > 1:
        return [*reasons, f"hosts disagree on HEAD: {heads}"], None
    resolved = next(iter(old)) if old else None
    if resolved and _git("cat-file", "-e", f"{resolved}^{{commit}}").returncode:
        return [*reasons, f"OLD {resolved} is not in {REPO}: git fetch origin"], None
    if resolved:
        stat = _git("diff", "--stat", resolved, new, "--", *_WATCHED).stdout.rstrip()
        s.say(stat or "no change under " + " ".join(_WATCHED))
        python = _git("diff", "--quiet", resolved, new, "--", ".python-version")
        if python.returncode and not args.allow_python_change:
            reasons.append(".python-version changes (venvs rebuild): pass --allow-python-change")
    return reasons, resolved


# ava:ref cli/docs/fleet-update-window.ava.okf.md#keep-decision-subprocess
def keep_decision(
    s: Session, old: str | None, new: str, *, timeout: float = KEEP_TIMEOUT_S
) -> list[str]:
    if old is None:
        old = new
    script = REPO / "scripts" / "host_ops" / "fleet_update_keep.py"
    argv = [sys.executable, str(script), "--repo", str(REPO), "--old", old, "--new", new]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        s.say(f"WARNING: keep decision timed out after {timeout:g}s; restarting every service")
        return []
    if done.returncode:
        detail = (done.stderr.strip().splitlines() or ["no output"])[-1]
        s.say(
            f"WARNING: keep decision failed (exit {done.returncode}: {detail}); "
            "restarting every service"
        )
        return []
    try:
        data = json.loads(done.stdout.strip().splitlines()[-1])
        keeps = [str(name) for name in data["keep_services"]]
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        s.say(f"WARNING: keep decision output was not usable ({exc!r}); restarting every service")
        return []
    s.say(f"keep decision: {old[:12]}..{new[:12]} ({data.get('changed_files', '?')} changed files)")
    for component in data.get("components", []):
        verdict = "keep" if component.get("keep") else "restart"
        hits = [str(hit) for hit in component.get("hits") or ()]
        detail = f"{component.get('hit_count', 0)} changed: " + ", ".join(hits[:3])
        if len(hits) > 3:
            detail += f", +{len(hits) - 3} more"
        if not hits:
            detail = str(component.get("reason", ""))
        s.say(f"keep decision: {component.get('name')}={verdict} ({detail})")
    return keeps


# ava:ref cli/docs/fleet-update-window.ava.okf.md#stored-schedules-refusal
def _stored_schedules(s: Session, args: argparse.Namespace, new: str) -> None:
    out = s.run(
        args.gateway,
        PRE_VERIFY.format(
            home=HOME,
            new=new,
            read_rows=shlex.quote(read_rows_py()),
            check_rows=shlex.quote(check_rows_py()),
        ),
        effect=False,
    )
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


def silence_program() -> str:
    return Path(__file__).with_name("fleet_alert_silence.py").read_text()


# ava:ref cli/docs/fleet-update-window.ava.okf.md#silence-window
def _silence(s: Session, args: argparse.Namespace, verb: str, comment: str = "") -> None:
    hours = getattr(args, "silence_hours", 0)
    tail = f" --hours {hours:g} --comment {shlex.quote(comment)}" if verb == "open" else ""
    out = s.run(args.gateway, f"{_PYTHON} - {verb}{tail}", stdin=silence_program())
    for line in out.splitlines():
        if line.startswith(("SILENCE failed", "SILENCE skipped")):
            s.say(f"WARNING: alert silence not {verb}: {line.removeprefix('SILENCE ')}")


# ava:ref cli/docs/fleet-update.ava.okf.md#update-half-ordering
def down(s: Session, args: argparse.Namespace) -> None:
    new = _git("rev-parse", "--verify", f"{args.new}^{{commit}}").stdout.strip()
    if not new:
        raise FailedError(f"refused: {args.new} is not a commit in {REPO}; git fetch origin")
    reasons, old = _preflight(s, args, new)
    if reasons:
        raise FailedError("refused: " + "; ".join(reasons))
    everyone = [args.gateway, *args.runner]
    for alias in everyone:
        s.run(alias, f'{HOME}; git cat-file -e "{new}^{{commit}}" || git fetch -q origin {new}')
    _stored_schedules(s, args, new)
    if args.restart_all:
        keeps: list[str] = []
        s.say("keep decision: skipped (--restart-all): every service stops")
    else:
        keeps = keep_decision(s, old, new)
    keep_flags = "".join(f" --keep-service {name}" for name in keeps)
    _silence(
        s, args, "open", f"fleet update to {new[:12]}: planned outage; `up` expires this silence"
    )
    for alias in [*args.runner, args.gateway]:
        try:
            s.run(alias, f"{HOME}; {AVA} stop -y --timeout {args.stop_timeout}{keep_flags}")
            if not s.dry_run:
                status, phase = split_hold_text(s.run(alias, _STATUS, effect=False))
                if (status, phase) != ("paused", "stopped"):
                    raise FailedError(f"{alias}: stop left {status}/{phase}")
        except FailedError as exc:
            raise FailedError(f"{exc}{_STOP_HINT}") from exc
    for alias in everyone:
        s.run(
            alias,
            f"{HOME}; git checkout -q --detach {new} && {_VENV_DIRS} && "
            f"env -u VIRTUAL_ENV uv sync --frozen --compile-bytecode && {CLEAN}",
        )
    s.say(f"down complete: every host is stopped on {new[:12]}; run `up` when ready")


def gui_oneshot(alias: str, timeout_s: int) -> str:
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%S}"
    label = f"com.ava.fleet-update.{re.sub(r'[^A-Za-z0-9.-]', '-', alias)}.{stamp}"
    log = f"/tmp/{label}/oneshot.log"
    start = f"{HOME}; {AVA} start"
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


def machine_names(s: Session, aliases: list[str]) -> dict[str, str]:
    ask = f'{_PYTHON} -c "from base.config import ConfigBoot; from base.cluster.machine.boot import machine_from_boot; print(machine_from_boot(ConfigBoot()).name())"'
    names: dict[str, str] = {}
    for alias in aliases:
        reported = s.run(alias, ask, effect=False).split()
        if not reported:
            raise FailedError(f"{alias}: the host printed no machine name")
        names[alias] = reported[-1]
    if len(set(names.values())) != len(names):
        raise FailedError(f"two listed hosts report one machine name: {names}")
    return names


# ava:ref cli/docs/fleet-update.ava.okf.md#up-completion-gates
def _roster(s: Session, args: argparse.Namespace, program: str) -> tuple[list[dict[str, Any]], str]:
    names = machine_names(s, [args.gateway, *args.runner])
    wanted = set(names.values())
    deadline = time.monotonic() + args.roster_timeout
    while True:
        out = s.run(args.gateway, f"{program} roster", effect=False, stdin=gateway_program())
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
        time.sleep(POLL_S)


def _report_unlisted(s: Session, rows: list[dict[str, Any]], wanted: set[str]) -> None:
    for row in rows:
        if row["name"] not in wanted:
            s.say(
                f"roster: {row['name']} is not listed, not checked: online={row['online']} "
                f"head={row['head_sha']} running={row['running_sha']}"
            )


def _refresh(s: Session, args: argparse.Namespace) -> None:
    for alias in [args.gateway, *args.runner]:
        out = s.run(alias, f"{HOME}; {AVA} packages refresh")
        if out:
            s.say(f"packages refresh [{alias}] {out.splitlines()[-1].strip()}")


def _drift_checks(s: Session, args: argparse.Namespace) -> None:
    checks = [(args.gateway, "schedules verify --no-notify")]
    checks += [(alias, "plugins verify") for alias in [args.gateway, *args.runner]]
    failed: list[str] = []
    for alias, verb in checks:
        try:
            s.run(alias, f"{HOME}; {AVA} {verb}", effect=True)
        except FailedError as exc:
            failed.append(f"`ava {verb}` {exc}")
    if failed:
        raise FailedError("drift check failed: " + "; ".join(failed))


def up(s: Session, args: argparse.Namespace) -> None:
    for alias in [args.gateway, *args.runner]:
        macos = s.os[alias] == "Darwin"
        s.run(alias, gui_oneshot(alias, args.start_timeout) if macos else f"{HOME}; {AVA} start")
        if (
            not s.dry_run
            and (hold := split_hold_text(s.run(alias, _STATUS, effect=False)))[0] not in _FREE
        ):
            raise FailedError(f"{alias}: start left maintenance {hold}")
    program = f"{_PYTHON} -"
    if s.dry_run:
        s.run(args.gateway, f"{program} smoke MACHINE {args.smoke_timeout}  # each agent-runner")
        _refresh(s, args)
        _drift_checks(s, args)
        _silence(s, args, "close")
        return
    rows, commit = _roster(s, args, program)
    for name in [row["name"] for row in rows if row["serve_agent_runner"]]:
        smoke = f"{program} smoke {shlex.quote(name)} {args.smoke_timeout}"
        s.run(args.gateway, smoke, stdin=gateway_program())
    _refresh(s, args)
    _drift_checks(s, args)
    _silence(s, args, "close")
    s.say(f"up complete: every listed machine runs {commit[:12]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cli.fleet_update",
        description="Update a networked source-mode cluster over SSH, in two halves",
    )
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
                "--silence-hours",
                type=float,
                default=4.0,
                help="expiry of the alert silence opened for the window (`up` expires it sooner)",
            )
            p.add_argument(
                "--allow-red-schedules",
                action="store_true",
                help="proceed although stored schedule scripts fail NEW's `ava schedules verify`",
            )
            p.add_argument(
                "--restart-all",
                action="store_true",
                help="skip the keep decision and stop every service, untouched components included",
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
