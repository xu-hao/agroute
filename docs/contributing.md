# Contributor workflow

Track every repository change with a descriptive issue, and report its number with a
sentence describing the work. Develop and validate in an issue-specific worktree from
`main`, preserving unrelated changes. Issue #1 specifies the multi-agent router and
`howhard` integration; issues #2–#7 specify its package skeleton, native quota tracking,
candidate emission, execution plan resolution, native plugin manifests, and behavioral
acceptance tests.

```sh
git worktree add -b issue-N-description ../agroute-issue-N main
cd ../agroute-issue-N
python -m venv .venv
.venv/bin/python -m pip install -e '.[test,docs]'
agc run --label "agroute tests" --resource cpu=1 -- .venv/bin/python -m pytest
```

Use the local AGCoord installation for checks and declare resources. Do not submit nested
coordinator commands from admitted jobs. Tests use synthetic configs and state directories
in their own temporary directories, exercise public behavior, and own all state they create.

Update the affected canonical documents, list new documents in the index, and record
user-facing changes in `CHANGELOG.md`. `AGENTS.md` is the canonical instruction source;
`CLAUDE.md` is its relative symlink.

The complete validation command is `scripts/check`. It runs tests, builds both distributions,
verifies wheel and sdist contents/versions in a clean environment, and builds Sphinx with
warnings treated as errors. Gate and publish through a single `agc land` request using that
command when a change is ready to land. Never push directly to the target branch or bypass
the coordinator verdict. Documentation-only changes follow the narrower exemption in the
root guardrails. End changed work with the [session handoff](session_handoff_format.md).
