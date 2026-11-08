# Packaging and release

The distribution, Python package, and console command are all `agroute`. The version's
single source is `src/agroute/__init__.py`; setuptools reads it dynamically, `--version`
prints it, and Sphinx uses the same value. Keep the version fields in `plugin.json`,
`.claude-plugin/plugin.json`, and `.antigravity-plugin/plugin.json` in sync with that Python
version; `scripts/verify-wheel.py` checks them in the source archive.

Before release:

1. Set the intended stable package version and add a dated matching changelog section in
   `CHANGELOG.md`.
2. Land the exact release commit through `agc land`, with `scripts/check` as its gate.
   The gate runs the behavioral test suite, builds the wheel and source distribution,
   verifies the installed wheel in a clean environment outside the checkout, and builds the
   canonical Sphinx documentation with `-W`.
3. Tag the verified commit `vX.Y.Z` and create a GitHub release when requested, attaching
   the verified wheel, sdist, and `SHA256SUMS`.
4. Upload to PyPI only if the user explicitly authorizes that upload. Implementation,
   tests, commits, pushes, merges, tags, and GitHub releases do not authorize PyPI.
5. After an authorized PyPI upload, verify the published PyPI file hashes against
   `SHA256SUMS` and test installing `agroute==X.Y.Z` in a clean environment.
