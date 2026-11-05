"""No emoji or shortcode literal below the adapter (RT-6.6, arc42 §5.1).

Mattermost shows symbols; the code names meanings (model.Mark, Answer,
StatusMark) and symbols.py, the one Python module that knows the characters,
writes them as escapes. This test finds an emoji character in a string literal
of the Python sources, or on a code line of the shell scripts and the OpenCode
plugin. Docstrings, comments and the runtime texts in Markdown are text, not
code, and stay as they are.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿️⃣]")
# The hooks.json description is read by humans in the plugin list, not code.
SHELL = [*sorted((ROOT / "bin").glob("*.sh")), *sorted((ROOT / "hooks").glob("*.sh"))]
TS = [*sorted((ROOT / "opencode").rglob("*.ts"))]
TS = [f for f in TS if "node_modules" not in f.parts]


def python_literals(source: str) -> list[str]:
    """String literals with an emoji written as a character, by line (docstrings aside)."""
    tree = ast.parse(source)
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            segment = ast.get_source_segment(source, n) or ""
            if EMOJI.search(segment):
                out.append(f"{n.lineno}: {segment[:40]!r}")
    return out


def code_lines(source: str, comment: str) -> list[str]:
    return [f"{i}: {line.strip()[:60]}" for i, line in enumerate(source.split("\n"), 1)
            if EMOJI.search(line) and not line.lstrip().startswith(comment)]


def test_the_sources_hold_no_emoji_literal():
    src = ROOT / "mcp" / "src" / "mattermost_ai_agent_bus"
    found = {f.name: python_literals(f.read_text()) for f in sorted(src.glob("*.py"))}
    found |= {f.name: code_lines(f.read_text(), "#") for f in SHELL if f.name != "mm-agent-symbols.sh"}
    found |= {f.name: code_lines(f.read_text(), "//") for f in TS}
    assert {name: hits for name, hits in found.items() if hits} == {}


def test_the_guard_sees_a_literal():
    source = ('"""Docstring with a crown \U0001F451."""\n'
              'x = "\U0001F451 hello"\n'
              'y = "\\U0001F451"  # an escape is fine\n'
              'z = f"{x} ✅"\n')
    assert [h.split(":")[0] for h in python_literals(source)] == ["2", "4"]
    assert code_lines("a=1\n# \U0001F451 comment\nb=\"\U0001F451\"\n", "#") == ['3: b="\U0001F451"']
