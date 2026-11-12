# Working in this repo

## Docs move with the code

The behaviour spec lives in [docs/arc42](docs/arc42) (arc42, LEAN depth). A change
is not done until the docs say what the code now does:

- **Which doc?** [docs/doc-map.toml](docs/doc-map.toml) maps code paths to the docs
  they must update. CI fails a PR that changes a mapped path without one of its
  docs; a change that truly needs none gets the `no-doc-change` label.
- **Runtime texts are product, not docs.** `SKILL.md`, the MCP `instructions` in
  `mcp/src/…/server.py`, the hook `reason` texts and the `hooks/hooks.json`
  description are read by agents at runtime. Update them with the behaviour.
- **Scenario ids.** Section 6 scenarios are `RT-6.x` (their section number).
  Reference the id from the code that implements a scenario, from `SKILL.md`,
  and from at least one test in `tests/hooks/` (hook behaviour) or `mcp/tests/`
  (MCP server behaviour). A new scenario needs a test.
- **Describe once, link elsewhere.** README, adapter docs and `docs/protocol.md`
  link to the arc42 section instead of restating behaviour.
- **Version bump → CHANGELOG.md entry** for that version, and the same version in
  `.claude-plugin/plugin.json` (the one doc-check reads), `mcp/pyproject.toml`,
  `mcp/src/…/__init__.py` and `mcp/uv.lock` (doc-check compares the first three).
- **Which number?** SemVer from 0.72.0. Minor: a new tool, argument or
  command, a noticeably changed behaviour, anything breaking (under
  `### Breaking` in its CHANGELOG entry). Patch: fixes that change no
  behaviour, docs, tests.

## Releasing

A version reaches hosts as a release tag (ADR-011, RT-6.29); every tag is signed,
since hosts that pin the release keys install nothing else. After the
merge the owner, and only the owner, creates the annotated SSH-signed tag
`v<version>` (the version in `.claude-plugin/plugin.json`) on the merge commit
and pushes it: `git tag -s v<version> -m v<version> <merge commit>`
(`gpg.format=ssh`, the owner's release key), checks it with
`git verify-tag v<version>` (`gpg.ssh.allowedSignersFile` with the pinned keys),
then `git push origin v<version>`. Never `-f`: a tag is never moved or re-signed
(hosts refuse a moved tag). Agents never sign and never create or push version
tags. `MM_RELEASE_URL` as a local path is for a mirror and the tests.

## Before you push

```bash
python3 scripts/arc42-lint/arc42-lint.py docs/arc42 --strict
python3 scripts/doc-check.py --base origin/main
uv run --directory mcp pytest -q
uv run --directory mcp pytest -q ../tests/hooks
git ls-files -z '*.sh' '*.sh.example' | xargs -0 shellcheck -S warning
uvx ruff@0.17.2 check
uv run --directory mcp mypy
```

`scripts/arc42-lint/` is vendored from MSiccDev/arc42-toolkit — do not edit it;
own checks go into `scripts/doc-check.py`.
