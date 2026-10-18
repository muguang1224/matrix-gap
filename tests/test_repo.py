import json
import unittest

from common import mg, make_repo, cleanup, run_main, run_json


def codes(report):
    return sorted({f.code for f in report.findings})


def find(report, code):
    return [f for f in report.findings if f.code == code]


CLEAN = {
    "pyproject.toml": '''[project]
name = "demo"
requires-python = ">=3.10"
classifiers = [
  "Programming Language :: Python :: 3.10",
  "Programming Language :: Python :: 3.11",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13",
  "Programming Language :: Python :: 3.14",
  "Programming Language :: Python :: 3.15",
]
''',
    ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12", "3.13", "3.14", "3.15"]
''',
}

GAPPY = {
    "pyproject.toml": '''[project]
requires-python = ">=3.9"
classifiers = [
  "Programming Language :: Python :: 3.9",
  "Programming Language :: Python :: 3.10",
  "Programming Language :: Python :: 3.11",
  "Operating System :: Microsoft :: Windows",
  "Operating System :: POSIX :: Linux",
]
''',
    ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9", "3.10", "3.11"]
''',
}


class EndToEndTests(unittest.TestCase):
    def analyze(self, files, **kwargs):
        root = make_repo(files)
        cleanup(self, root)
        return mg.analyze(root, **kwargs)

    def test_clean_repo_has_no_findings(self):
        report = self.analyze(CLEAN)
        self.assertEqual(report.findings, [])

    def test_missing_versions_and_os(self):
        report = self.analyze(GAPPY)
        self.assertEqual([f.message for f in find(report, "MG001")],
                         ["Python 3.12 is claimed but not tested",
                          "Python 3.13 is claimed but not tested",
                          "Python 3.14 is claimed but not tested",
                          "Python 3.15 is claimed but not tested"])
        self.assertEqual([f.message for f in find(report, "MG004")],
                         ["windows is claimed but no CI job runs there"])

    def test_no_workflow(self):
        report = self.analyze({"pyproject.toml": '[project]\nrequires-python = ">=3.9"\n'})
        self.assertIn("MG009", codes(report))
        self.assertTrue(any("no workflow" in f.message for f in report.findings))

    def test_classifier_contradiction(self):
        report = self.analyze({
            "pyproject.toml": '''[project]
requires-python = ">=3.9,<3.12"
classifiers = ["Programming Language :: Python :: 3.12"]
''',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9", "3.10", "3.11"]
''',
        })
        self.assertIn("MG002", codes(report))

    def test_go_below_minimum(self):
        report = self.analyze({
            "go.mod": "module x\n\ngo 1.22\n",
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-go@v5
        with:
          go-version: "1.21"
''',
        })
        messages = [f.message for f in report.findings]
        self.assertTrue(any("requires 1.22" in m for m in messages))
        self.assertNotIn("MG003", codes(report))

    def test_rust_msrv_not_tested(self):
        report = self.analyze({
            "Cargo.toml": '[package]\nname = "x"\nrust-version = "1.70"\n',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: dtolnay/rust-toolchain@stable
        with:
          toolchain: stable
''',
        })
        self.assertIn("MG011", codes(report))

    def test_node_gap_only_lts_majors(self):
        report = self.analyze({
            "package.json": '{"name":"x","engines":{"node":">=18"}}',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        node-version: [18, 20, 22]
''',
        })
        gaps = [f.message for f in find(report, "MG001")]
        self.assertEqual(gaps, ["Node 24 is claimed but not tested",
                                "Node 26 is claimed but not tested"])

    def test_eol_is_opt_in(self):
        files = {
            "pyproject.toml": '[project]\nrequires-python = ">=3.9,<3.10"\n',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9"]
''',
        }
        plain = self.analyze(files)
        self.assertNotIn("MG008", codes(plain))
        with_eol = self.analyze(files, use_eol=True, today="2026-10-18")
        self.assertIn("MG008", codes(with_eol))

    def test_os_independent_is_not_a_claim(self):
        report = self.analyze({
            "pyproject.toml": '''[project]
requires-python = ">=3.10,<3.11"
classifiers = ["Operating System :: OS Independent"]
''',
            ".github/workflows/ci.yml": '''jobs:
  t:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"
''',
        })
        self.assertNotIn("MG004", codes(report))
        self.assertNotIn("MG009", codes(report))

    def test_os_claims_covered_by_block_list(self):
        report = self.analyze({
            "setup.cfg": '''[metadata]
classifiers =
    Operating System :: Microsoft :: Windows
    Operating System :: POSIX :: Linux
[options]
python_requires = >=3.10,<3.11
''',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on:
      - ubuntu-latest
      - windows-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"
''',
        })
        self.assertNotIn("MG004", codes(report))
        self.assertNotIn("MG001", codes(report))

    def test_tox_brace_envlist_counts_as_coverage(self):
        report = self.analyze({
            "pyproject.toml": '[project]\nrequires-python = ">=3.9,<3.12"\n',
            "tox.ini": "[tox]\nenvlist = py{39,310,311}\n",
        })
        self.assertEqual(set(report.coverage["python"]), {"3.9", "3.10", "3.11"})
        self.assertNotIn("MG001", codes(report))

    def test_tox_range_and_dotted_envlist(self):
        report = self.analyze({
            "pyproject.toml": '[project]\nrequires-python = ">=3.9,<3.13"\n',
            "tox.ini": "[tox]\nenvlist = py3.9, py3{10-12}\n",
        })
        self.assertEqual(set(report.coverage["python"]),
                         {"3.9", "3.10", "3.11", "3.12"})

    def test_cargo_workspace_msrv(self):
        report = self.analyze({
            "Cargo.toml": '[workspace.package]\nrust-version = "1.70"\n',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: dtolnay/rust-toolchain@stable
        with:
          toolchain: stable
''',
        })
        self.assertIn("MG011", codes(report))

    def test_go_directive_with_comment(self):
        report = self.analyze({
            "go.mod": "module x\n\ngo 1.22 // minimum supported\n",
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-go@v5
        with:
          go-version: "1.22"
''',
        })
        self.assertNotIn("MG009", codes(report))
        self.assertNotIn("MG001", codes(report))

    def test_matrix_level_expression_does_not_claim_no_ci(self):
        report = self.analyze({
            "pyproject.toml": '[project]\nrequires-python = ">=3.10"\n',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}
''',
        })
        self.assertNotIn("MG009", codes(report))
        self.assertIn("MG005", codes(report))

    def test_symbolic_os_does_not_suppress_python_gap(self):
        report = self.analyze({
            "pyproject.toml": '''[project]
requires-python = ">=3.10,<3.12"
classifiers = ["Operating System :: POSIX :: Linux"]
''',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        os: ["self-hosted"]
        python-version: ["3.10"]
''',
        })
        self.assertIn("MG001", codes(report))
        self.assertNotIn("MG004", codes(report))

    def test_dynamic_env_scalar_is_reported_as_dynamic(self):
        report = self.analyze({
            "pyproject.toml": '[project]\nrequires-python = ">=3.10"\n',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PY }}
''',
        })
        self.assertNotIn("MG009", codes(report))
        self.assertIn("MG005", codes(report))

    def test_rust_toolchain_action_ref_covers_msrv(self):
        report = self.analyze({
            "Cargo.toml": '[package]\nname = "x"\nrust-version = "1.70"\n',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: dtolnay/rust-toolchain@1.70.0
''',
        })
        self.assertNotIn("MG011", codes(report))

    def test_poetry_metadata_is_read(self):
        report = self.analyze({
            "pyproject.toml": '''[tool.poetry]
name = "x"
python = "^3.9"
classifiers = ["Programming Language :: Python :: 3.9"]
''',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9"]
''',
        })
        self.assertTrue(any(c.kind == "requires-python" and "3.9" in c.versions
                            for c in report.claims))
        self.assertIn("MG001", codes(report))

    def test_setup_py_python_requires(self):
        report = self.analyze({
            "setup.py": 'from setuptools import setup\nsetup(name="x", python_requires=">=3.9,<3.11")\n',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9", "3.10"]
''',
        })
        self.assertNotIn("MG001", codes(report))

    def test_tox_bare_range_envlist(self):
        report = self.analyze({
            "pyproject.toml": '[project]\nrequires-python = ">=3.9,<3.13"\n',
            "tox.ini": "[tox]\nenvlist = 3{9-12}\n",
        })
        self.assertEqual(set(report.coverage["python"]),
                         {"3.9", "3.10", "3.11", "3.12"})

    def test_tox_inline_comment_is_not_an_env(self):
        report = self.analyze({
            "pyproject.toml": '[project]\nrequires-python = ">=3.9,<3.12"\n',
            "tox.ini": "[tox]\nenvlist = py39,py310  # py312 pending\n",
        })
        self.assertNotIn("3.12", report.coverage.get("python", {}))
        self.assertIn("MG001", codes(report))

    def test_unquoted_scalar_version_is_not_coverage(self):
        report = self.analyze({
            "pyproject.toml": '[project]\nrequires-python = ">=3.10,<3.11"\n',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: 3.10
''',
        })
        self.assertNotIn("3.10", report.coverage.get("python", {}))
        self.assertIn("MG006", codes(report))

    def test_classifier_below_candidate_floor_is_not_a_contradiction(self):
        report = self.analyze({
            "pyproject.toml": '''[project]
requires-python = ">=3.6"
classifiers = ["Programming Language :: Python :: 3.6"]
''',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.6"]
''',
        })
        self.assertNotIn("MG002", codes(report))

    def test_impossible_date_is_tool_error(self):
        root = make_repo({"pyproject.toml": '[project]\nrequires-python = ">=3.9"\n'})
        cleanup(self, root)
        code, _ = run_main(root, "--eol", "--today", "2026-02-30")
        self.assertEqual(code, 2)

    def test_include_only_matrix_is_coverage(self):
        report = self.analyze({
            "pyproject.toml": '[project]\nrequires-python = ">=3.10,<3.12"\n',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        include:
          - python-version: "3.10"
          - python-version: "3.11"
''',
        })
        self.assertNotIn("MG009", codes(report))
        self.assertNotIn("MG001", codes(report))

    def test_symbolic_coverage_is_not_reported_as_missing(self):
        report = self.analyze({
            "pyproject.toml": '[project]\nrequires-python = ">=3.10"\n',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.x"]
''',
        })
        self.assertNotIn("MG009", codes(report))
        self.assertIn("MG005", codes(report))

    def test_node_26_is_not_outside_claimed_range(self):
        report = self.analyze({
            "package.json": '{"name":"x","engines":{"node":">=18"}}',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        node-version: [18, 20, 22, 24, 26]
''',
        })
        self.assertEqual(find(report, "MG003"), [])

    def test_bad_eol_file_is_tool_error(self):
        root = make_repo({
            "pyproject.toml": '[project]\nrequires-python = ">=3.9"\n',
            "eol.json": "{not json",
        })
        cleanup(self, root)
        code, _ = run_main(root, "--eol-file", root / "eol.json")
        self.assertEqual(code, 2)

    def test_bad_today_is_tool_error(self):
        root = make_repo({"pyproject.toml": '[project]\nrequires-python = ">=3.9"\n'})
        cleanup(self, root)
        code, _ = run_main(root, "--eol", "--today", "not-a-date")
        self.assertEqual(code, 2)

    def test_json_is_ascii_safe(self):
        root = make_repo({
            "pyproject.toml": '[project]\nrequires-python = ">=3.9"\n',
            ".github/workflows/ci.yml": "jobs:\n  t:\n    runs-on: ubuntu-latest\n",
        })
        cleanup(self, root)
        code, payload = run_json(root)
        self.assertEqual(code, 1)
        self.assertIn("claims", payload)
        report = self.analyze({
            "pyproject.toml": '[project]\nrequires-python = ">=3.10,<3.12"\n',
            "tox.ini": "[tox]\nenvlist = py310,py311\n",
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10"]
''',
        })
        self.assertNotIn("MG001", codes(report))

    def test_dynamic_matrix_suppresses_missing_version_findings(self):
        report = self.analyze({
            "pyproject.toml": '[project]\nrequires-python = ">=3.10"\n',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ${{ fromJSON(needs.plan.outputs.versions) }}
''',
        })
        self.assertNotIn("MG001", codes(report))
        self.assertIn("MG005", codes(report))

    def test_json_shape(self):
        root = make_repo(GAPPY)
        cleanup(self, root)
        code, payload = run_json(root)
        self.assertEqual(code, 1)
        self.assertEqual(payload["tool"], "matrix-gap")
        self.assertEqual(payload["schema_version"], 1)
        for key in ("claims", "ci", "findings", "summary", "warnings"):
            self.assertIn(key, payload)
        self.assertEqual(payload["summary"]["high"], 4)
        self.assertTrue(all("code" in f and "severity" in f for f in payload["findings"]))

    def test_min_severity_does_not_change_exit_code(self):
        files = {
            "package.json": '{"name":"x"}',
            ".github/workflows/ci.yml": '''jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: [3.10]
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
''',
        }
        root = make_repo(files)
        cleanup(self, root)
        code_high, out_high = run_main(root, "--min-severity", "high")
        code_low, out_low = run_main(root, "--min-severity", "low", "--fail-on", "low")
        self.assertEqual(code_high, 0)   # only low findings, default fail-on high
        self.assertEqual(code_low, 1)    # fail-on low sees the same low finding
        self.assertIn("MG006", out_low)
        self.assertNotIn("MG006", out_high)

    def test_fail_on_none_is_always_zero(self):
        root = make_repo(GAPPY)
        cleanup(self, root)
        code, _ = run_main(root, "--fail-on", "none")
        self.assertEqual(code, 0)

    def test_missing_path_is_tool_error(self):
        code, _ = run_main("/definitely/not/here")
        self.assertEqual(code, 2)

    def test_empty_directory_is_clean(self):
        root = make_repo({})
        cleanup(self, root)
        code, _ = run_main(root)
        self.assertEqual(code, 0)

    def test_warnings_do_not_crash_on_bad_json(self):
        report = self.analyze({"package.json": "{not json"})
        self.assertTrue(any("could not parse JSON" in w for w in report.warnings))

    def test_deduplicates_identical_claims(self):
        report = self.analyze({
            "pyproject.toml": '''[project]
classifiers = [
  "Programming Language :: Python :: 3.9",
  "Programming Language :: Python :: 3.9",
]
''',
            ".github/workflows/ci.yml": "jobs:\n  t:\n    runs-on: ubuntu-latest\n",
        })
        claims = [c for c in report.claims if c.kind == "classifier"]
        self.assertEqual(len(claims), 1)


if __name__ == "__main__":
    unittest.main()
