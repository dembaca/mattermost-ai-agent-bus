#!/usr/bin/env python3
"""Keep code, runtime texts and docs/arc42 in step.

    python3 scripts/doc-check.py                     # static checks only
    python3 scripts/doc-check.py --base origin/main  # plus the doc map
    python3 scripts/doc-check.py --base origin/main --skip-doc-map

Complements the vendored arc42 linter (scripts/arc42-lint/), which checks the
docs against each other. This checks what it cannot see:

  doc-map    a changed code path has its doc changed too (docs/doc-map.toml)
  blocks     Section 6 participants are building blocks from Section 5
  rt-ids     every RT-6.x referenced anywhere exists as a Section 6 scenario,
             and every scenario is referenced by at least one test in tests/ or mcp/tests/
  changelog  the plugin version has an entry in CHANGELOG.md, and its
             `Needs:` and `Withdrawn:` lines keep their grammar (RT-6.30)
  versions   the MCP package (pyproject.toml, __version__) has the plugin version

Exit 0 when clean, 1 on any error. Stdlib only.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARC42 = ROOT / "docs" / "arc42"
SEC5 = ARC42 / "05-building-block-view.md"
SEC6 = ARC42 / "06-runtime-view.md"
DOC_MAP = ROOT / "docs" / "doc-map.toml"
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
CHANGELOG = ROOT / "CHANGELOG.md"
PYPROJECT = ROOT / "mcp" / "pyproject.toml"
PKG_INIT = ROOT / "mcp" / "src" / "mattermost_ai_agent_bus" / "__init__.py"

# Files that may mention RT ids without being a reference (the checker itself).
RT_SCAN_EXCLUDE = ("scripts/doc-check.py",)

RT_REF = re.compile(r"\bRT-(6\.\d+)\b")

errors: list[tuple[str, int | None, str]] = []


def error(path: Path | str, line: int | None, msg: str) -> None:
    rel = str(Path(path).resolve().relative_to(ROOT)) if isinstance(path, Path) else path
    errors.append((rel, line, msg))


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def repo_files() -> list[str]:
    out = git("ls-files", "--cached", "--others", "--exclude-standard")
    return [f for f in out.splitlines() if (ROOT / f).is_file()]


# --- doc-map ----------------------------------------------------------------

def check_doc_map(base: str) -> None:
    merge_base = git("merge-base", base, "HEAD").strip()
    changed = set(git("diff", "--name-only", merge_base).splitlines())
    changed |= set(git("ls-files", "--others", "--exclude-standard").splitlines())
    rules = tomllib.loads(DOC_MAP.read_text())["rule"]
    for rule in rules:
        hits = sorted(
            f for f in changed if any(fnmatch.fnmatch(f, p) for p in rule["paths"])
        )
        if hits and not any(d in changed for d in rule["docs"]):
            error(
                hits[0], None,
                f"doc-map [{rule['name']}]: {', '.join(hits)} changed, but none of "
                f"{', '.join(rule['docs'])} — update the doc, or mark the change "
                "with the no-doc-change label",
            )


# --- blocks -------------------------------------------------------------------

def section5_blocks() -> set[str]:
    names, in_table = set(), False
    for line in SEC5.read_text().splitlines():
        if re.match(r"^\|\s*Name\s*\|", line):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cell = line.split("|")[1].strip()
            if cell and not set(cell) <= set("-: "):
                names.add(cell)
    return names


def check_blocks() -> None:
    blocks = section5_blocks()
    for n, line in enumerate(SEC6.read_text().splitlines(), 1):
        m = re.match(r"^\*\*Participants:\*\*\s*(.+)$", line)
        if not m:
            continue
        internal = m.group(1).split(";")[0]
        for name in (p.strip() for p in internal.split(",")):
            if name and name not in blocks:
                error(SEC6, n, f'participant "{name}" is not a Section 5 building block '
                               f"({', '.join(sorted(blocks))}); put externals after ';'")


# --- rt-ids -------------------------------------------------------------------

def scenarios() -> dict[str, int]:
    found = {}
    for n, line in enumerate(SEC6.read_text().splitlines(), 1):
        m = re.match(r"^##\s+(6\.\d+)\s", line)
        if m:
            found[m.group(1)] = n
    return found


def check_rt_ids() -> None:
    defined = scenarios()
    tested: set[str] = set()
    for f in repo_files():
        if f in RT_SCAN_EXCLUDE or f.startswith("scripts/arc42-lint/"):
            continue
        try:
            text = (ROOT / f).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for ref in RT_REF.findall(line):
                if ref not in defined:
                    error(f, n, f"RT-{ref} is not a scenario in {SEC6.relative_to(ROOT)}")
                elif f.startswith(("tests/", "mcp/tests/")):
                    tested.add(ref)
    for ref, n in defined.items():
        if ref not in tested:
            error(SEC6, n, f"scenario RT-{ref} has no test referencing it in tests/ or mcp/tests/")


# --- changelog ------------------------------------------------------------------

def check_changelog() -> None:
    version = json.loads(PLUGIN.read_text())["version"]
    if not CHANGELOG.exists() or not re.search(
        rf"^##\s+\[?{re.escape(version)}\]?(\s|$)", CHANGELOG.read_text(), re.MULTILINE
    ):
        error(CHANGELOG, None, f"no entry for plugin version {version}")


# The release rules (ADR-011 point 3, RT-6.30; CLAUDE.md, "Releasing"): a
# `Needs:` line at the start of a line in a version's entry says what that
# release needs first, in variable names, versions and these words only, at
# most NEEDS_MAX characters, since it reaches every agent's context; a
# `Withdrawn:` line names older releases. Kept alike in bin/mm-agent-lib.sh
# (MM_NEEDS_WORDS) and mcp/src/mattermost_ai_agent_bus/release.py.
NEEDS_WORDS = frozenset(
    "alike all and before corral deployed dispatcher every first host hosts new on or "
    "registrar removed renamed restart server servers session sessions set same the to "
    "unset update".split())
NEEDS_MAX = 120
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
WITHDRAWN_RE = re.compile(r"^Withdrawn: (v\d+\.\d+\.\d+(?:, v\d+\.\d+\.\d+){0,9})$")


def needs_ok(text: str) -> bool:
    if not text or len(text) > NEEDS_MAX or not re.fullmatch(r"[A-Za-z0-9_., ]+", text):
        return False
    return all(re.fullmatch(r"MM_[A-Z0-9_]+", w) or VERSION_RE.match(w) or w in NEEDS_WORDS
               for w in (w.removesuffix(",") for w in text.split()))


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def check_release_lines(text: str | None = None) -> None:
    entry = ""
    for n, line in enumerate((CHANGELOG.read_text() if text is None else text).splitlines(), 1):
        if line.startswith("## "):
            m = re.match(r"^## \[?(\d+\.\d+\.\d+)\]?\s*$", line)
            entry = m.group(1) if m else ""
        elif line.startswith("Needs:"):
            if not entry:
                error(CHANGELOG, n, "a Needs: line outside a version's entry")
            elif not line.startswith("Needs: ") or not needs_ok(line[7:]):
                error(CHANGELOG, n, "Needs: takes MM_* variables, versions and the fixed words of "
                      f"scripts/doc-check.py (NEEDS_WORDS), at most {NEEDS_MAX} characters")
        elif line.startswith("Withdrawn:"):
            m = WITHDRAWN_RE.match(line)
            if not entry:
                error(CHANGELOG, n, "a Withdrawn: line outside a version's entry")
            elif not m:
                error(CHANGELOG, n, "Withdrawn: takes v<x.y.z>[, v<x.y.z>…] (at most ten)")
            elif any(_vkey(v[1:]) >= _vkey(entry) for v in m.group(1).split(", ")):
                error(CHANGELOG, n, f"Withdrawn: in {entry} names a release not older than {entry}")


# --- versions -------------------------------------------------------------------

def check_versions() -> None:
    version = json.loads(PLUGIN.read_text())["version"]
    pyproject = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    if pyproject != version:
        error(PYPROJECT, None, f"version {pyproject}, plugin is {version}")
    m = re.search(r'^__version__ = "([^"]+)"', PKG_INIT.read_text(), re.MULTILINE)
    if not m or m.group(1) != version:
        error(PKG_INIT, None, f"__version__ {m.group(1) if m else 'missing'}, plugin is {version}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", help="git ref to diff against for the doc map")
    ap.add_argument("--skip-doc-map", action="store_true",
                    help="the change needs no doc update (no-doc-change)")
    args = ap.parse_args()

    if args.base and not args.skip_doc_map:
        check_doc_map(args.base)
    check_blocks()
    check_rt_ids()
    check_changelog()
    check_release_lines()
    check_versions()

    gh = os.environ.get("GITHUB_ACTIONS") == "true"
    for path, line, msg in errors:
        if gh:
            loc = f"file={path}" + (f",line={line}" if line else "")
            print(f"::error {loc}::{msg}")
        else:
            print(f"{path}{f':{line}' if line else ''}: {msg}")
    if errors:
        print(f"doc-check: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print("doc-check: ok", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
