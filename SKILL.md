---
name: matrix-gap
description: Audit claimed platform support against the CI matrix that actually tests it. Use when a repository claims Python/Node/Go/Rust versions or operating systems that its GitHub Actions matrix may not cover.
---

# matrix-gap

Static auditor for the gap between **what a repository claims to support** and **what CI actually tests**. Zero dependencies, no network, no edits.

## When to use

- A user asks whether their CI matrix matches `requires-python`, classifiers, `engines.node`, `rust-version`, `go.mod`, or `tox.ini`.
- Before a release, to check that “supports X” is backed by a job.
- When a workflow was edited and someone wants to know which version lines lost coverage.
- When reviewing an AI-generated CI change that quietly removed a matrix entry.

## Run

```bash
python3 matrix_gap.py <repo>              # human report
python3 matrix_gap.py <repo> --json       # stable machine report
python3 matrix_gap.py <repo> --min-severity high --fail-on high
python3 matrix_gap.py <repo> --eol        # also flag EOL versions
```

Exit codes: `0` clean at the fail threshold, `1` findings, `2` tool error.

## How to read the result

- `MG001` / `MG009` mean a claim has no concrete test. Check whether the version is still meant to be supported; if not, narrow the claim or add a matrix entry.
- `MG002` is an internal contradiction between a classifier and `requires-python`.
- `MG003` means CI spends time on a version the package does not claim.
- `MG004` is an OS claim without a runner.
- `MG005` is **not** a gap: the matrix or value is dynamic and was deliberately not guessed. Do not “fix” it by inventing versions.
- `MG006` is a real YAML trap: quote `3.10`, otherwise YAML reads `3.1`.
- `MG008` only appears with `--eol`; the bundled dates are best-effort and should be verified against endoflife.date.

## Guardrails

- Treat the report as evidence, not a mandate. A gap can be intentional (for example, a newly released Python that the project will add next week).
- Never edit the target repository as part of an audit. Report findings and propose a diff separately.
- Do not add a matrix entry for a version the project does not actually support just to silence `MG001`; fix the claim instead when that is the truth.
- Do not quote dynamic matrices as “untested”. `MG005` is the honest answer.
- When suggesting YAML, quote every version string.

## Output contract

`--json` emits `schema_version: 1` with `claims`, `ci.jobs`, `ci.coverage`, `findings`, `warnings`, and `summary`. Findings carry `code`, `severity`, `ecosystem`, `message`, `detail`, `source`, and `line`. Prefer this shape when another tool consumes the result.
