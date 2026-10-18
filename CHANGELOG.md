# Changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog, and the project uses semantic versioning.

## [0.1.0] - 2026-10-18

### Added

- `matrix-gap` CLI: static audit of claimed support vs GitHub Actions coverage.
- Claim readers: PEP 621 `pyproject.toml`, Poetry metadata, `setup.cfg`,
  `setup.py`, `package.json`, `Cargo.toml` (including workspace MSRV),
  `go.mod`, and `tox.ini`.
- GitHub Actions matrix reader with axes, flow/block lists, `include`,
  `exclude`, `runs-on` forms, `setup-*` scalar versions, `*-version-file`,
  action refs such as `dtolnay/rust-toolchain@1.70.0`, block scalars, CRLF,
  quoted job keys, indentless sequences, anchors and aliases.
- Findings MG001–MG011 with stable JSON output (`schema_version: 1`),
  independent `--min-severity` / `--fail-on`, and opt-in EOL checks.
- 134 unittest cases; the repository dogfoods itself in CI.

### Fixed during adversarial verification

The first release was tested by three independent adversarial reviewers. The
following real defects were found and fixed before publishing:

- Flow-style one-line `include` / `exclude` was discarded, producing false
  coverage.
- `matrix: ${{ fromJSON(...) }}` was treated as “no matrix”, producing a false
  high-severity MG009.
- A `with: python-version:` input from a reusable-workflow step was counted as
  coverage; `with:` before `uses:` was not.
- Include-only matrices recorded zero coverage because `CiJob.coverage()`
  only iterated axis keys.
- Indentless YAML sequences, quoted job keys, runner-named OS axes, and
  matrix anchors were silently ignored.
- A symbolic value in one axis (for example `os: [self-hosted]`) suppressed
  gaps in every ecosystem.
- PEP 440 major-only bounds (`<4`) voided a spec; patch bounds (`>=3.9.5`,
  `!=3.9.0`) removed whole minors; `==3.*` inverted.
- TOML array-of-tables aborted the whole file on Python < 3.11, hiding all
  claims; dotted keys were ignored.
- tox brace/range envlists (`py{39,310}`, `3{9-12}`) and inline comments were
  mishandled.
- Node `>= 18`, `18 - 20`, and Node 26 were misparsed or misreported.
- EOL inputs were not validated; non-ASCII stdout could crash under an ASCII
  encoding.
