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
             and every scenario is referenced by at least one test in tests/
  changelog  the plugin version has an entry in CHANGELOG.md

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
                elif f.startswith("tests/"):
                    tested.add(ref)
    for ref, n in defined.items():
        if ref not in tested:
            error(SEC6, n, f"scenario RT-{ref} has no test referencing it in tests/")


# --- changelog ------------------------------------------------------------------

def check_changelog() -> None:
    version = json.loads(PLUGIN.read_text())["version"]
    if not CHANGELOG.exists() or not re.search(
        rf"^##\s+\[?{re.escape(version)}\]?(\s|$)", CHANGELOG.read_text(), re.MULTILINE
    ):
        error(CHANGELOG, None, f"no entry for plugin version {version}")


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
