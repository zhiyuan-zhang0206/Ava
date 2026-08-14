#!/usr/bin/env python3
"""R2-B legacy skill-identity migration: one skill = one key = one dir = one row.

Scans the three identity surfaces the runtime now enforces, reports every
inconsistency, and (with `--apply`) fixes them with the *directory name as the
identity source* (design R2-B, "directory name is authoritative"): the SKILL.md frontmatter `name:`
is the display declaration and must fold to the directory name; a registry row
must fold to exactly one key; a stored config reference must resolve against
the load-dir catalog.

Surface 1 — skills load dir (`$AVA_HOME/skills/`): every SKILL.md's frontmatter
name vs its load-dir leaf directory name. A mismatch is a skill that presents
under a name that is not its own; the runtime loader now refuses it
(`ava.skills.SkillIdentityMismatch`), so it must be fixed before the next scan.

Surface 2 — install registry (`$AVA_HOME/installed.json`): rows that fold to
the same key (dash and underscore are one name). The registry read now refuses
this state (`shared.install_registry.DuplicatePackageName`).

Surface 3 — DB stores (optional, `AVA_DB_URL` or `--db-url`): the skill lists
(`skills_to_inject_into_system_prompt`, `skills_to_expand_at_start`) in
`agents_meta.config_overlay`, `agents_meta.birth_config`, and
`agent_presets.config`. An entry that resolves to nothing against the catalog
is reported; `--apply` rewrites it through the mapping derived from Surface-1
fixes (old frontmatter name -> new display name), e.g. `wechat` -> `wechat-ocr`.
The 405-ruled transforms (2026-08-08) are mirrored here and in the rollout SQL
the v0.1.0 baseline (formerly migration 20260808T075000, squashed into db/schema.sql at the 2026-08-14 reset): bare `ava_code` / `ava-code` expands to the four
sub-skill identifiers and `telegram` is dropped — reported as DECIDED and
applied under `--apply`.

`--check` (default) is read-only and exits 1 when anything is inconsistent.
`--apply` fixes, with a `.bak-<timestamp>` copy of every touched file and a
printout of every DB row it would change (DB writes are still explicit
SQL statements the operator can review; the tool runs them itself).

Run on a host whose `$AVA_HOME` you are allowed to mutate. Never run against
the repo checkout (repo skills are gated at merge by
scripts/lint_skill_descriptions.py instead).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# shared.skill_names is importable standalone (no heavy deps).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.skill_names import display_name, match_key

try:
    from shared.install_registry import Registry, registry_lock
except Exception:  # pragma: no cover - importable in the repo venv
    Registry = None
    registry_lock = None

_SKILL_LIST_FIELDS = ("skills_to_inject_into_system_prompt", "skills_to_expand_at_start")
# 405 ruling 2026-08-08: bare ava_code was never a skill (namespace with no
# root SKILL.md) — the stored lists meant the family, which is these four.
_AVA_CODE_SUBSKILLS = (
    "ava-code:worktree",
    "ava-code:pr",
    "ava-code:testing",
    "ava-code:conventions",
)
# Dead reference: the telegram skill was removed from the repo; the IM
# bridge is the only Telegram frontend.
_DROP_REFS = frozenset({"telegram"})


def _transform_skill_list(entries: list[str], mapping: dict[str, str]) -> list[str]:
    """Apply the identity-config-refs transforms to one skill list, in
    order, deduplicated: bare `ava_code` / `ava-code` expands to the four
    sub-skills, `_DROP_REFS` entries are dropped, and `mapping` renames
    (e.g. `wechat` -> `wechat-ocr`). Mirrors the rollout SQL migration
    20260808T075000 so the operator tool and the deploy path cannot drift.
    """
    out: list[str] = []
    for entry in entries:
        if entry in ("ava_code", "ava-code"):
            for sub in _AVA_CODE_SUBSKILLS:
                if sub not in out:
                    out.append(sub)
            continue
        if entry in _DROP_REFS:
            continue
        renamed = mapping.get(entry, entry)
        if renamed not in out:
            out.append(renamed)
    return out


_NAME_RE = re.compile(r"^name:\s*(.+)$", re.M)


def _fold(s: str) -> str:
    return s.replace("-", "_").replace(":", ".")


def scan_skills_dir(skills_root: Path) -> list[dict[str, Any]]:
    """Every SKILL.md whose frontmatter name does not fold to its load-dir
    leaf directory name. Follows symlinked directories (a user-installed
    skill is commonly a link into another tree)."""
    findings = []
    if not skills_root.is_dir():
        return findings
    for dirpath, _dirnames, filenames in os_walk_follow(skills_root):
        if "SKILL.md" not in filenames:
            continue
        smd = Path(dirpath) / "SKILL.md"
        text = smd.read_text(encoding="utf-8", errors="replace")
        m = _NAME_RE.search(text)
        if not m:
            continue  # missing name is the format lint's job, not identity's
        fm = m.group(1).strip()
        leaf = Path(dirpath).name
        if _fold(fm) != _fold(leaf):
            findings.append(
                {
                    "kind": "frontmatter-dir-mismatch",
                    "file": str(smd),
                    "dir": leaf,
                    "frontmatter": fm,
                    "fix_to": display_name(leaf),
                }
            )
    return findings


def os_walk_follow(root: Path):
    import os

    return os.walk(root, followlinks=True)


def catalog(skills_root: Path, fixes: dict[str, str] | None = None) -> dict[str, str]:
    """match_key(identifier) -> canonical identifier, plus bare frontmatter
    keys — the same two lookups `resolve_prompt_skills` uses, so a stored
    reference either resolves the same way or is reported the same way.

    `fixes` maps a load-dir leaf directory name to its corrected frontmatter
    name (the `fix_to` of a frontmatter-dir mismatch): the DB surface must be
    judged against the POST-fix catalog, because a reference like `wechat`
    resolves today and only stops resolving once the frontmatter is renamed
    to `wechat-ocr` — that is exactly the reference the apply step rewrites."""
    by_key: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os_walk_follow(skills_root):
        if "SKILL.md" not in filenames:
            continue
        smd = Path(dirpath) / "SKILL.md"
        text = smd.read_text(encoding="utf-8", errors="replace")
        m = _NAME_RE.search(text)
        if not m:
            continue
        rel = Path(dirpath).relative_to(skills_root).parts
        leaf = rel[-1] if rel else None
        # A mount-point root SKILL.md (empty rel) has no directory name to
        # fold against — its frontmatter name is the identity unchanged.
        name = (fixes or {}).get(leaf, m.group(1).strip()) if leaf else m.group(1).strip()
        ident = ":".join(display_name(seg) for seg in (*rel[:-1], name))
        by_key.setdefault(match_key(ident), ident)
        by_key.setdefault(match_key(name), ident)
    return by_key


def scan_registry(registry_path: Path) -> list[dict[str, Any]]:
    """Registry rows that fold to the same key."""
    if Registry is None:
        return [{"kind": "registry-unreadable", "error": "shared.install_registry unavailable"}]
    if not registry_path.exists():
        return []
    try:
        raw = registry_path.read_text(encoding="utf-8")
        if not raw.strip():
            return []
        registry = Registry.model_validate_json(raw)
    except Exception as e:
        return [{"kind": "registry-unreadable", "error": str(e)}]
    seen: dict[str, str] = {}
    dups: list[dict[str, Any]] = []
    for pkg in registry.packages:
        key = match_key(pkg.name)
        prev = seen.get(key)
        if prev is not None:
            dups.append({"kind": "registry-duplicate-row", "names": [prev, pkg.name]})
        else:
            seen[key] = pkg.name
    return dups


def _db_query(db_url: str, sql: str) -> list[tuple]:
    # psql is a fixed binary; db_url comes from the operator's env / CLI, and
    # sql is built by this module (see apply_db for the escaping). check=False
    # is deliberate: the caller inspects r.returncode to distinguish query
    # failures from empty results.
    r = subprocess.run(  # noqa: S603 - fixed binary, operator-supplied args
        ["psql", db_url, "-t", "-A", "-F", "\t", "-c", sql],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()[:500]}")
    rows = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        rows.append(tuple(line.split("\t")))
    return rows


def scan_db(db_url: str, by_key: dict[str, str]) -> list[dict[str, Any]]:
    """Skill-list entries across the three stores that resolve to nothing."""
    findings = []
    checks = [
        ("agents_meta", "id", "config_overlay"),
        ("agents_meta", "id", "birth_config"),
        ("agent_presets", "name", "config"),
    ]
    for table, id_col, col in checks:
        # Identifiers come only from the constant `checks` tuple above — no
        # user input reaches the query text.
        sql = (
            f"SELECT {id_col}, {col} FROM {table} "  # noqa: S608 - identifiers are compile-time constants
            f"WHERE jsonb_typeof({col}) = 'object'"
        )
        try:
            rows = _db_query(db_url, sql)
        except RuntimeError as e:
            findings.append({"kind": "db-unreadable", "table": table, "error": str(e)})
            continue
        for ident, raw in rows:
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for field in _SKILL_LIST_FIELDS:
                lst = obj.get(field)
                if not isinstance(lst, list):
                    continue
                for entry in lst:
                    if not isinstance(entry, str):
                        continue
                    if entry == "*":
                        continue
                    if match_key(entry) not in by_key:
                        findings.append(
                            {
                                "kind": "unresolved-config-ref",
                                "table": table,
                                "id": ident,
                                "field": field,
                                "entry": entry,
                            }
                        )
    return findings


def _fix_mapping(findings: list[dict[str, Any]]) -> dict[str, str]:
    """old frontmatter name -> new display name, from the frontmatter fixes
    (the only source of renames the tool is allowed to make)."""
    return {
        f["frontmatter"]: f["fix_to"]
        for f in findings
        if f["kind"] == "frontmatter-dir-mismatch" and f["frontmatter"] != f["fix_to"]
    }


def apply_frontmatter(findings: list[dict[str, Any]], *, backup_dir: Path) -> list[str]:
    """Rewrite each mismatched frontmatter name to the directory's display
    name (dir authoritative). Backs up every touched file first."""
    done = []
    for f in findings:
        if f["kind"] != "frontmatter-dir-mismatch":
            continue
        path = Path(f["file"])
        text = path.read_text(encoding="utf-8")
        new_text = _NAME_RE.sub(f"name: {f['fix_to']}", text, count=1)
        if new_text == text:
            continue
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_dir / f"{path.name}.{stamp}.bak"
        shutil.copy2(path, backup)
        path.write_text(new_text, encoding="utf-8")
        done.append(f"{path}: name -> {f['fix_to']} (backup {backup})")
    return done


def apply_registry(registry_path: Path, dups: list[dict[str, Any]]) -> list[str]:
    """Merge folding-duplicate rows, keeping the display-canonical spelling
    (the row whose name is already the dash form, e.g. `ava-code` over
    `ava_code`; first row wins on a tie).

    This is the one registry writer outside `shared.install_registry`, and it
    re-reads the file INSIDE the lock rather than trusting the `dups` its caller
    scanned earlier: between that scan and here, another process may have merged
    the same rows already, in which case the filter below simply finds nothing to
    drop. Two reasons it must hold the same lock the module's own writers take —
    the read-modify-write can lose their rows, and it stages through the SAME
    fixed temp name (`installed.json.tmp`), so a concurrent `save` would have its
    staged body overwritten and its rename left with nothing to rename.
    """
    if not dups or Registry is None or registry_lock is None:
        return []
    with registry_lock(registry_path):
        raw = registry_path.read_text(encoding="utf-8")
        registry = Registry.model_validate_json(raw) if raw.strip() else Registry()
        drop_names: set[str] = set()
        merged: list[str] = []
        for d in dups:
            a, b = d["names"]
            a_canon, b_canon = a == display_name(a), b == display_name(b)
            keep, drop = (a, b) if (a_canon and not b_canon) or (a_canon == b_canon) else (b, a)
            # Drop by RAW name, not folded key: both rows share the key, so a
            # key-based filter would remove the survivor too.
            drop_names.add(drop)
            merged.append(f"{a} / {b} -> {keep}")
        registry.packages = [p for p in registry.packages if p.name not in drop_names]
        body = json.dumps(json.loads(registry.model_dump_json()), indent=2) + "\n"
        tmp = registry_path.with_name(f"{registry_path.name}.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(registry_path)
    return merged


def _sql_literal(value: str) -> str:
    """Render a string as a SQL literal: single quotes doubled. Every value
    interpolated into the rewrite SQL is a skill name from disk / frontmatter
    (operator- or author-controlled), so nothing may pass through unescaped."""
    return "'" + value.replace("'", "''") + "'"


def apply_db(db_url: str, mapping: dict[str, str]) -> list[str]:
    """Rewrite the skill-list entries across the three stores / two fields —
    the same transforms as the rollout SQL migration 20260808T075000 (squashed into the v0.1.0 baseline at the 2026-08-14 reset): bare
    `ava_code` expands to the four sub-skills, `telegram` is dropped, and
    `mapping` renames (e.g. `wechat` -> `wechat-ocr`). Idempotent. Each
    rewrite prints the exact UPDATE it runs."""
    done = []
    targets = (
        ("agents_meta", "config_overlay"),
        ("agents_meta", "birth_config"),
        ("agent_presets", "config"),
    )
    for table, col in targets:
        for field in _SKILL_LIST_FIELDS:
            # Read the list, transform in Python (the operator tool's mirror
            # of the migration's SQL), write back only when it changed.
            sql = (
                f"SELECT {col} FROM {table} "  # noqa: S608 - identifiers constant, values escaped
                f"WHERE jsonb_typeof({col} -> {_sql_literal(field)}) = 'array'"
            )
            rows = _db_query(db_url, sql)
            for (raw,) in rows:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                lst = obj.get(field)
                if not isinstance(lst, list) or not any(isinstance(e, str) for e in lst):
                    continue
                new_lst = _transform_skill_list(lst, mapping)
                if new_lst == lst:
                    continue
                update_sql = (
                    f"UPDATE {table} SET {col} = jsonb_set("  # noqa: S608 - identifiers constant, values escaped
                    f"{col}, ARRAY[{_sql_literal(field)}], "
                    f"{_sql_literal(json.dumps(new_lst, ensure_ascii=False))}::jsonb) "
                    f"WHERE {col} = {_sql_literal(raw)}::jsonb"
                )
                r = subprocess.run(  # noqa: S603 - fixed binary; SQL escaped above
                    ["psql", db_url, "-c", update_sql],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if r.returncode != 0:
                    raise RuntimeError(f"DB rewrite failed: {r.stderr.strip()[:500]}")
                done.append(f"{table}.{col}[{field}] {lst} -> {new_lst}")
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="fix findings (default: check only)")
    ap.add_argument(
        "--ava-home", default=None, help="target AVA_HOME (default: env AVA_HOME / ~/.ava)"
    )
    ap.add_argument(
        "--db-url", default=None, help="postgres URL for the DB surface (default: env AVA_DB_URL)"
    )
    ap.add_argument("--no-db", action="store_true", help="skip the DB surface entirely")
    args = ap.parse_args(argv)

    home = Path(args.ava_home or os.environ.get("AVA_HOME") or Path.home() / ".ava")
    skills_root = home / "skills"
    registry_path = home / "installed.json"

    dir_findings = scan_skills_dir(skills_root)
    reg_findings = scan_registry(registry_path)
    db_findings: list[dict[str, Any]] = []
    db_url = args.db_url or os.environ.get("AVA_DB_URL", "")
    if db_url and not args.no_db:
        fixes = {
            f["dir"]: f["fix_to"] for f in dir_findings if f["kind"] == "frontmatter-dir-mismatch"
        }
        db_findings = scan_db(db_url, catalog(skills_root, fixes))
    elif not db_url and not args.no_db:
        print("(DB surface skipped: no AVA_DB_URL / --db-url)", file=sys.stderr)

    findings = dir_findings + reg_findings + db_findings
    print(f"skill-identity check on {home}")
    for f in findings:
        if f["kind"] == "frontmatter-dir-mismatch":
            print(
                f"  MISMATCH {f['file']}: frontmatter name {f['frontmatter']!r} "
                f"does not fold to directory {f['dir']!r} -> fix to {f['fix_to']!r}"
            )
        elif f["kind"] == "registry-duplicate-row":
            print(f"  DUPLICATE ROW {f['names'][0]!r} / {f['names'][1]!r} fold to one key")
        elif f["kind"] == "unresolved-config-ref":
            mapping = _fix_mapping(dir_findings)
            hint = (
                f" -> auto-fix to {mapping[f['entry']]!r} with --apply"
                if f["entry"] in mapping
                else " (no mechanical mapping — decide manually)"
            )
            print(f"  UNRESOLVED {f['table']} id={f['id']} {f['field']}: {f['entry']!r}{hint}")
        elif f["kind"] == "decided-config-ref":
            action = (
                f"expand to {len(_AVA_CODE_SUBSKILLS)} sub-skills"
                if f["entry"] in ("ava_code", "ava-code")
                else "drop (dead reference)"
            )
            print(
                f"  DECIDED {f['table']} id={f['id']} {f['field']}: {f['entry']!r} -> {action} "
                "(405 ruling; rollout migration 20260808T075000 — squashed into the v0.1.0 baseline — --apply mirrors)"
            )
        elif f["kind"] in ("registry-unreadable", "db-unreadable"):
            print(f"  ERROR {f.get('table', 'registry')}: {f['error']}")

    if not findings:
        print("clean: no skill-identity inconsistencies")
        return 0
    if not args.apply:
        print(f"\n{len(findings)} finding(s) — re-run with --apply to fix", file=sys.stderr)
        return 1

    backup_dir = home / f"skill-identity-backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    applied: list[str] = []
    applied += apply_frontmatter(dir_findings, backup_dir=backup_dir)
    applied += apply_registry(registry_path, reg_findings)
    mapping = _fix_mapping(dir_findings)
    if db_url and not args.no_db and mapping:
        applied += apply_db(db_url, mapping)
    print("applied:")
    for line in applied:
        print(f"  {line}")
    print(f"backups: {backup_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
