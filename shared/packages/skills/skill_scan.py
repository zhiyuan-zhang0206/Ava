"""Install-time static scan of a third-party skill package.

A skill package is data an agent is instructed to *follow*, shipped with
whatever scripts it bundles. Installing one from a public index is a supply-chain
event: the ClawHub audit that found ~341 malicious skills delivering a macOS
stealer is the shape this module exists for. So every package entering
`$AVA_HOME/skills/` from outside the repo is read first, file by file, and
matched against a rule table — after any encoded blob it carries is decoded,
recursively, since base64-wrapping the payload was that campaign's primary
evasion.

Two severities, and only one of them is a gate:

- **critical** — a pattern with no ordinary explanation inside an instruction
  pack: a download-and-execute pipeline, an obfuscated payload, a credential
  path paired with an outbound sink in the same file, instructions aimed at
  making the reading agent work behind its user's back, or text hidden from a
  human reviewer. The install refuses. Nothing lands on disk.
- **notice** — a capability worth a reviewer's eye but routinely legitimate
  (`sudo`, `rm -rf`, an AWS credential path in a deploy skill, an HTTP POST).
  Never blocks; printed, and the material a human reads before promoting a
  package to the `reviewed` trust tier.

This is a mitigation layer, not a boundary — the same honesty `ava/security.py`
and `SECURITY.md` apply to everything else here. It matches *known shapes of
known attacks* in *text this scanner can read*. A clean report means "no rule
fired", never "this package is safe". Anyone who knows these rules can write a
package that walks past all of them, and nothing here constrains what a skill
does once an agent follows it (Ava does not sandbox execution — see
`decisions/2026-07-29-security-model-host-isolation-not-sandbox.md`).

Sibling, deliberately not shared: `ava/security.py` scans content an agent
ingests *mid-turn* and reports through a side channel. This one scans a whole
package tree *before* it exists on disk, needs file/line anchoring, and answers
a yes/no install question. The two tables overlap on hidden-text patterns only;
merging them is also blocked by the layer rule (`shared` < `ava`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Severity = Literal["critical", "notice"]

# Files big enough that scanning them is pointless (a skill is markdown and
# small scripts) and anything non-text: both are reported, never silently
# skipped — an unreadable blob inside an instruction pack is itself the finding.
_MAX_SCAN_BYTES = 1_000_000
_BINARY_SNIFF_BYTES = 4096

# Directories that are never package content.
_SKIP_DIRS = frozenset({".git", "__pycache__", ".DS_Store", "node_modules", ".venv"})


@dataclass(frozen=True)
class Rule:
    """One pattern the scanner matches, with the reason a reader needs to judge
    it. `id` is stable — it is what `--accept-risk` records in the registry."""

    id: str
    severity: Severity
    pattern: re.Pattern[str]
    why: str


@dataclass(frozen=True)
class Finding:
    """One rule match, anchored to a file and line inside the package."""

    rule_id: str
    severity: Severity
    why: str
    path: str  # relative to the package root
    line: int
    excerpt: str


# Spelled as escapes, not literals: these are invisible in a source file too.
_ZERO_WIDTH_CLASS = "[\u200b\u200c\u200d\u2060\ufeff]"


def _rx(p: str) -> re.Pattern[str]:
    return re.compile(p, re.IGNORECASE)


# ── critical: no ordinary explanation inside a skill package ────────────────

_CRITICAL: tuple[Rule, ...] = (
    Rule(
        "remote-code-execution",
        "critical",
        _rx(r"\b(curl|wget)\b[^\n|]{0,200}\|\s*(sudo\s+)?(ba|z|k|fi)?sh\b"),
        "downloads a script and pipes it straight into a shell",
    ),
    Rule(
        "remote-code-execution",
        "critical",
        _rx(r"\b(ba|z|k)?sh\b\s*<\(\s*\b(curl|wget)\b"),
        "executes a downloaded script via process substitution",
    ),
    Rule(
        "remote-code-execution",
        "critical",
        _rx(r"\b(curl|wget)\b[^\n|]{0,200}\|\s*(python3?|node|perl|ruby|php)\b"),
        "downloads code and pipes it into an interpreter",
    ),
    Rule(
        "remote-code-execution",
        "critical",
        _rx(
            r"\b(iwr|invoke-webrequest|invoke-restmethod|irm)\b[^\n|]{0,200}\|\s*(iex|invoke-expression)\b"
        ),
        "PowerShell download-and-execute",
    ),
    Rule(
        "obfuscated-payload",
        "critical",
        _rx(r"\bbase64\b[^\n|]{0,80}(-d|-D|--decode)[^\n|]{0,80}\|\s*(ba|z|k)?sh\b"),
        "base64-decodes a blob and pipes it into a shell",
    ),
    Rule(
        "obfuscated-payload",
        "critical",
        _rx(r"\b(eval|exec)\s*\(\s*[^)\n]{0,120}\bb(ase)?64decode\b"),
        "evaluates base64-decoded code at runtime",
    ),
    Rule(
        "obfuscated-payload",
        "critical",
        _rx(r"\b(eval|new\s+Function)\s*\(\s*[^)\n]{0,120}\batob\s*\("),
        "evaluates base64-decoded code at runtime (JavaScript)",
    ),
    Rule(
        "obfuscated-payload",
        "critical",
        _rx(r"['\"][A-Za-z0-9+/]{200,}={0,2}['\"]"),
        "carries a long base64 literal — an embedded payload, not readable instructions",
    ),
    Rule(
        "obfuscated-payload",
        "critical",
        _rx(r"(\\x[0-9a-f]{2}){40,}"),
        "carries a long hex-escaped string — an embedded payload",
    ),
    Rule(
        "safety-subversion",
        "critical",
        _rx(
            r"\b(do\s*n[o']?t|never|avoid)\s+(tell|telling|mention|mentioning|inform|informing|notify|notifying|show|showing)\b[^\n]{0,40}\bthe\s+user\b"
        ),
        "instructs the agent to keep its actions from the user",
    ),
    Rule(
        "safety-subversion",
        "critical",
        _rx(r"\bwithout\s+(asking|telling|informing|notifying|confirming\s+with)\s+the\s+user\b"),
        "instructs the agent to act without the user's knowledge",
    ),
    Rule(
        "safety-subversion",
        "critical",
        _rx(r"\bhide\s+(this|it|that|the\s+\w+)\s+from\s+(the\s+user|logs?)\b"),
        "instructs the agent to hide its actions",
    ),
    Rule(
        "safety-subversion",
        "critical",
        _rx(
            r"\b(ignore|disregard|override|disable|bypass|skip)\s+(your|the|all|any)\s+[\w\s]{0,20}\b(safety|security|guardrail|permission|approval|confirmation|restriction)"
        ),
        "instructs the agent to disable or route around its own safety layer",
    ),
    Rule(
        "safety-subversion",
        "critical",
        _rx(r"\b(disable|bypass|skip)\s+(your|the|all|any)\s+(safety|security)\s+hooks?\b"),
        "instructs the agent to disable a safety/security hook",
    ),
    Rule(
        "safety-subversion",
        "critical",
        _rx(
            r"\b(ignore|forget)\s+(all\s+)?(previous|prior|earlier|above)\s+(instructions?|prompts?|rules?)\b"
        ),
        "prompt-injection imperative aimed at the reading agent",
    ),
    Rule(
        "reverse-shell",
        "critical",
        _rx(r"/dev/tcp/[\w.$]+/\d+|\bnc\b[^\n]{0,60}\s-[a-z]{0,4}e\b|socat\b[^\n]{0,80}EXEC:"),
        "opens an interactive shell back to a remote host",
    ),
    Rule(
        "hidden-instruction",
        "critical",
        re.compile(_ZERO_WIDTH_CLASS),
        "contains zero-width characters — text a human reviewer cannot see",
    ),
    Rule(
        "forged-tool-call",
        "critical",
        _rx(r"</?(function_calls|antml:invoke|tool_calls?)>|\[system\s*(prompt)?\]"),
        "forges system / tool-call markup to impersonate the harness",
    ),
)

# Applied to markdown only. A comment in a bundled `.html` asset is a template
# note a human reads; a comment in a SKILL.md is text the *agent* reads and the
# reviewer's markdown renderer hides — the same words, a different risk. The
# keyword set is deliberately narrow (phrasings with no documentation reading),
# so a hidden instruction worded freshly gets past this: it catches known
# injection shapes, not "an instruction someone hid".
_MARKDOWN_CRITICAL: tuple[Rule, ...] = (
    Rule(
        "hidden-instruction",
        "critical",
        re.compile(
            r"<!--(?:(?!-->)[\s\S]){0,2000}?"
            r"\b(ignore\s+(all\s+|the\s+|your\s+)?(previous|prior|above)|system\s+prompt|"
            r"secretly|do\s*n[o']?t\s+(tell|mention|inform)|without\s+telling)\b"
            r"(?:(?!-->)[\s\S]){0,2000}?-->",
            re.IGNORECASE,
        ),
        "hides an instruction in a markdown comment — invisible to a human reviewer",
    ),
)

# ── notice: powerful, frequently legitimate, always worth a reviewer's eye ──

# A **store** of credentials on disk: the thing a stealer harvests wholesale.
# Alone this is a notice; paired with an outbound sink in the same file it is
# upgraded to `credential-exfiltration`.
#
# Environment-variable names are deliberately NOT here. A skill that reads
# `ANTHROPIC_API_KEY` and POSTs is usually just calling the API that key is for
# — Ava's own `ava-guide` and `ava-schedule-writer` do exactly that with
# `AVA_CLUSTER_SECRET` — so putting them in the co-occurrence rule made the
# gate fire on ordinary first-party skills. They match `secret-reference`
# (notice) instead.
_CREDENTIAL_STORE = _rx(
    r"("
    r"\.aws/credentials|\.ssh/id_[a-z0-9]+|\.config/gcloud|\.kube/config|"
    r"\.docker/config\.json|\.netrc|\.gnupg|\.npmrc|\.pypirc|\.git-credentials|"
    r"security\s+find-(generic|internet)-password|"
    r"login\.keychain|Keychains/|"
    r"Library/Application\s+Support/(Google/Chrome|Firefox|BraveSoftware)|"
    r"Local\s+State|Login\s+Data|cookies\.sqlite|"
    r"Exodus|Electrum|\.bitcoin|MetaMask|wallet\.dat|Ledger\s*Live|Trezor|Phantom|Keplr|"
    # The agent's own secrets file. ClawHavoc read ~/.clawdbot/.env; the Ava
    # analogue is the cluster secret sitting in $AVA_HOME/.env, which is the
    # single credential a skill has no business reading.
    r"\.ava/\.env|\.clawdbot/|\.hermes/\.env|\.claude\.json|\.codex/auth|"
    # Telegram Desktop's session dir, NOT ".telegram" — that substring lives
    # inside `api.telegram.org`.
    r"tdata/|Signal/sql"
    r")"
)

_SECRET_ENV = _rx(
    r"\b([A-Z][A-Z0-9]*_(API_KEY|SECRET|TOKEN|ACCESS_KEY|PASSWORD)|AWS_SECRET_ACCESS_KEY)\b"
)

# A way bytes leave the machine.
_NETWORK_SINK = _rx(
    r"("
    r"\bcurl\b[^\n]{0,200}(-X\s*POST|--data|-d\s|-F\s|--upload-file|-T\s)|"
    r"\bwget\b[^\n]{0,200}--post-|"
    r"\b(requests|httpx|aiohttp)\.(post|put)\s*\(|"
    r"\burllib\.request\.(urlopen|Request)\s*\(|"
    r"\bfetch\s*\([^\n]{0,120}method\s*:\s*['\"]POST|"
    r"\bnc\b\s+(-\w+\s+)*[\w.]+\s+\d{2,5}|"
    r"hooks\.slack\.com|discord(app)?\.com/api/webhooks|api\.telegram\.org|"
    r"\bhttps?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r")"
)

_NOTICE: tuple[Rule, ...] = (
    Rule(
        "credential-path",
        "notice",
        _CREDENTIAL_STORE,
        "reads a credential store on disk",
    ),
    Rule(
        "secret-reference",
        "notice",
        _SECRET_ENV,
        "reads a secret from the environment",
    ),
    Rule(
        "outbound-network",
        "notice",
        _NETWORK_SINK,
        "sends data off the machine",
    ),
    Rule(
        "privilege-escalation",
        "notice",
        _rx(
            r"\bsudo\s+\S|\bchmod\s+(777|\+s)\b|\bsetuid\b|osascript[^\n]{0,80}administrator privileges"
        ),
        "runs with elevated privileges",
    ),
    Rule(
        "destructive-command",
        "notice",
        _rx(
            r"\brm\s+-[rf]{1,2}\b|\bmkfs\.|\bdd\s+if=|\bDROP\s+(TABLE|DATABASE)\b|\bTRUNCATE\s+TABLE\b|\bgit\s+push\s+--force\b"
        ),
        "deletes or overwrites data irreversibly",
    ),
    Rule(
        "encrypted-archive",
        "notice",
        _rx(r"\b(zip|unzip|7z|7za)\b[^\n]{0,60}\s-(P|p)[\s\w]"),
        "password-protected archive — the shape used to carry a payload past antivirus",
    ),
    Rule(
        "persistence",
        "notice",
        _rx(r"\bcrontab\b|LaunchAgents|LaunchDaemons|systemd/user|\.bashrc|\.zshrc|\.profile"),
        "installs itself to run again later",
    ),
)


def _iter_files(root: Path) -> list[Path]:
    """Every file under `root` worth scanning, sorted, skipping VCS/build junk."""
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        out.append(p)
    return out


# ── encoded-payload pre-decoding ───────────────────────────────────────────
# ClawHavoc's primary evasion was wrapping the payload: the visible text is a
# base64 blob, and only the decoded bytes carry the download-and-execute line.
# Matching the wrapper catches the careless variant; decoding catches the rest.
# So every blob is decoded and the critical rules run against the *result* too,
# recursively (a payload wrapped twice is a payload).

_B64_BLOB = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")
_HEX_BLOB = re.compile(r"(?:\\x[0-9a-fA-F]{2}){8,}")
# Percent-escapes interleave with literal text rather than forming a solid run
# (`curl%20http%3A//host/x%20%7C%20sh`), so URL decoding is per line, gated on
# enough escapes that an ordinary link in prose does not qualify.
_URLENC_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_URLENC_MIN_ESCAPES = 4

_MAX_DECODE_DEPTH = 3
_MAX_BLOBS_PER_TEXT = 40


def _printable_ratio(s: str) -> float:
    return sum(c.isprintable() or c in "\n\t" for c in s) / len(s) if s else 0.0


def _decode_blobs(text: str) -> list[tuple[str, int, str]]:
    """Every decodable encoded blob in `text`, as `(decoded, line, encoding)`.

    A blob that decodes to mostly-unprintable bytes is not a hidden instruction,
    it is a checksum or an embedded image — dropped, so the caller does not scan
    binary noise. Bounded in count and recursion depth: this runs on hostile
    input, and a decode bomb is a denial-of-service on the install command.
    """
    import base64
    import binascii
    import urllib.parse

    out: list[tuple[str, int, str]] = []

    def keep(decoded: str, raw: str, line: int, kind: str) -> None:
        if len(decoded) >= 4 and decoded != raw and _printable_ratio(decoded) >= 0.9:
            out.append((decoded, line, kind))

    for rx, kind in ((_B64_BLOB, "base64"), (_HEX_BLOB, "hex")):
        for m in list(rx.finditer(text))[:_MAX_BLOBS_PER_TEXT]:
            raw = m.group(0)
            try:
                if kind == "base64":
                    decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True).decode(
                        "utf-8", errors="replace"
                    )
                else:
                    decoded = bytes.fromhex(raw.replace("\\x", "")).decode(
                        "utf-8", errors="replace"
                    )
            except (binascii.Error, ValueError):
                continue
            keep(decoded, raw, text.count("\n", 0, m.start()) + 1, kind)

    for lineno, line in enumerate(text.splitlines()[:_MAX_BLOBS_PER_TEXT], start=1):
        if len(_URLENC_ESCAPE.findall(line)) >= _URLENC_MIN_ESCAPES:
            keep(urllib.parse.unquote(line), line, lineno, "url")
    return out


def _scan_decoded(text: str, rel: str, depth: int = 1) -> list[Finding]:
    """Run the critical rules against everything `text` encodes, recursively.

    Findings anchor to the line of the *wrapper* in the real file — that is the
    line a reviewer has to look at — and say which encoding hid them.
    """
    if depth > _MAX_DECODE_DEPTH:
        return []
    found: list[Finding] = []
    for decoded, line, kind in _decode_blobs(text):
        for f in _scan_text(decoded, rel, _CRITICAL):
            found.append(
                Finding(
                    rule_id=f.rule_id,
                    severity="critical",
                    why=f"{f.why} — hidden in a {kind}-encoded blob",
                    path=rel,
                    line=line,
                    excerpt=f.excerpt,
                )
            )
        found += _scan_decoded(decoded, rel, depth + 1)
    return found


def _excerpt(line: str, match: re.Match[str]) -> str:
    """A short, terminal-safe quote of what matched. Control characters (which
    an attacker uses to repaint the terminal) become escapes, and zero-width
    hits — invisible by construction — render as their codepoint."""
    matched = match.group(0)
    # A zero-width hit has nothing to quote, so render its codepoints instead.
    text = line.strip() if matched.strip() else "".join(f"\\u{ord(c):04x}" for c in matched)
    text = "".join(c if c.isprintable() or c == " " else repr(c)[1:-1] for c in text)
    return text[:160] + ("…" if len(text) > 160 else "")


def _scan_text(text: str, rel: str, rules: tuple[Rule, ...]) -> list[Finding]:
    """Match `rules` against `text`, one finding per (rule id, line)."""
    lines = text.splitlines()
    seen: set[tuple[str, int]] = set()
    found: list[Finding] = []
    for rule in rules:
        for m in rule.pattern.finditer(text):
            lineno = text.count("\n", 0, m.start()) + 1
            if (rule.id, lineno) in seen:
                continue
            seen.add((rule.id, lineno))
            raw = lines[lineno - 1] if lineno <= len(lines) else ""
            found.append(
                Finding(
                    rule_id=rule.id,
                    severity=rule.severity,
                    why=rule.why,
                    path=rel,
                    line=lineno,
                    excerpt=_excerpt(raw, m),
                )
            )
    return found


def _exfiltration(text: str, rel: str) -> list[Finding]:
    """Upgrade the credential-path + outbound-sink co-occurrence to critical.

    Either half alone is ordinary (a deploy skill names `~/.aws/credentials`; a
    web skill POSTs). Both in one file is the ClawHavoc stealer shape, so it
    gates the install and a reviewer decides."""
    cred = _CREDENTIAL_STORE.search(text)
    sink = _NETWORK_SINK.search(text)
    if cred is None or sink is None:
        return []
    lines = text.splitlines()
    lineno = text.count("\n", 0, cred.start()) + 1
    sink_line = text.count("\n", 0, sink.start()) + 1
    return [
        Finding(
            rule_id="credential-exfiltration",
            severity="critical",
            why=(
                f"reads a credential store and sends data off the machine in the same "
                f"file (sink at line {sink_line})"
            ),
            path=rel,
            line=lineno,
            excerpt=_excerpt(lines[lineno - 1] if lineno <= len(lines) else "", cred),
        )
    ]


def scan_text_critical(
    text: str, *, path: str = "SKILL.md", markdown: bool = True
) -> list[Finding]:
    """Critical-only scan of ONE text blob — the runtime skill-loading gate
    (install-time `scan_package` scans a whole package tree before it lands on
    disk; this is the same rule table applied to a SKILL.md already on disk,
    at load time). Includes the encoded-blob pre-decoding, so a base64-wrapped
    payload is caught here too.

    Only the critical table runs — notice-level findings are the install-time
    reviewer's business and stay out of the runtime gate."""
    # A leading BOM is an encoding artifact (every Windows-authored file
    # carries one), not hidden text — same strip as `scan_package`; a BOM
    # anywhere else still counts as a zero-width hit.
    text = text.removeprefix("\ufeff")
    rules = _CRITICAL + (_MARKDOWN_CRITICAL if markdown else ())
    findings = _scan_text(text, path, rules)
    findings += _scan_decoded(text, path)
    return findings


def scan_package(root: Path) -> list[Finding]:
    """Every finding in the package tree at `root`, critical first.

    Reads each file as UTF-8; a file that is not decodable text, or is too large
    to be instructions, yields its own notice rather than being skipped."""
    findings: list[Finding] = []
    for p in _iter_files(root):
        rel = p.relative_to(root).as_posix()
        size = p.stat().st_size
        if size > _MAX_SCAN_BYTES:
            findings.append(
                Finding(
                    "unscannable-file",
                    "notice",
                    f"{size} bytes — too large to read as instructions, not scanned",
                    rel,
                    0,
                    "",
                )
            )
            continue
        raw = p.read_bytes()
        if b"\0" in raw[:_BINARY_SNIFF_BYTES]:
            findings.append(
                Finding(
                    "unscannable-file",
                    "notice",
                    "binary file bundled in a skill package — not scanned, read it yourself",
                    rel,
                    0,
                    "",
                )
            )
            continue
        try:
            # A leading BOM is an encoding artifact (every skill authored on
            # Windows carries one), not hidden text — strip it before the
            # zero-width rule sees it. A BOM anywhere else still counts.
            text = raw.decode("utf-8").removeprefix("\ufeff")
        except UnicodeDecodeError:
            findings.append(
                Finding("unscannable-file", "notice", "not valid UTF-8 — not scanned", rel, 0, "")
            )
            continue
        rules = _CRITICAL + (_MARKDOWN_CRITICAL if p.suffix.lower() == ".md" else ())
        findings += _scan_text(text, rel, rules)
        findings += _scan_decoded(text, rel)
        findings += _exfiltration(text, rel)
        findings += _scan_text(text, rel, _NOTICE)
    findings.sort(key=lambda f: (f.severity != "critical", f.path, f.line, f.rule_id))
    return findings


def criticals(findings: list[Finding]) -> list[Finding]:
    """The subset that gates an install."""
    return [f for f in findings if f.severity == "critical"]


def rule_ids(findings: list[Finding]) -> list[str]:
    """Sorted distinct rule ids — what `--accept-risk` records in the registry."""
    return sorted({f.rule_id for f in findings})


def render(findings: list[Finding], *, package: str) -> str:
    """The scan report a human reads before deciding. Empty findings render as
    an explicit 'nothing matched', never as silence."""
    if not findings:
        return f"  {package}: no rule matched (this is not a proof of safety)."
    lines = [f"  {package}:"]
    for f in findings:
        where = f"{f.path}:{f.line}" if f.line else f.path
        lines.append(f"    [{f.severity}] {f.rule_id} — {f.why}")
        lines.append(f"      {where}")
        if f.excerpt:
            lines.append(f"      | {f.excerpt}")
    return "\n".join(lines)
