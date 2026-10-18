import unittest

from common import mg, parse_workflow_file, make_repo, cleanup


def job(files, name=".github/workflows/ci.yml"):
    jobs, report = parse_workflow_file(files, name)
    return jobs, report


class MatrixParsingTests(unittest.TestCase):
    def test_flow_list(self):
        jobs, _ = job({".github/workflows/ci.yml": """
name: ci
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9", "3.10"]
"""})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].axes["python-version"], ["3.9", "3.10"])
        self.assertEqual(jobs[0].coverage()["python-version"], {"3.9", "3.10"})

    def test_block_list(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version:
          - "3.11"
          - "3.12"
"""})
        self.assertEqual(jobs[0].axes["python-version"], ["3.11", "3.12"])

    def test_include_adds_combination(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9"]
        os: [ubuntu-latest]
        include:
          - python-version: "3.12"
            os: ubuntu-latest
"""})
        self.assertEqual(jobs[0].coverage()["python-version"], {"3.9", "3.12"})

    def test_include_new_combination(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9"]
        include:
          - python-version: "3.12"
            experimental: true
"""})
        self.assertEqual(jobs[0].coverage()["python-version"], {"3.9", "3.12"})

    def test_exclude_removes_combination(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9", "3.10"]
        os: [ubuntu-latest]
        exclude:
          - python-version: "3.9"
            os: ubuntu-latest
"""})
        self.assertEqual(jobs[0].coverage()["python-version"], {"3.10"})

    def test_include_targets_matching_combination(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9", "3.10"]
        os: [ubuntu-latest, windows-latest]
        include:
          - os: windows-latest
            python-version: "3.10"
            experimental: true
"""})
        self.assertEqual(len(jobs[0].combos), 4)

    def test_dynamic_matrix(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ${{ fromJSON(needs.plan.outputs.versions) }}
"""})
        self.assertEqual(jobs[0].axes["python-version"], [])
        self.assertTrue(jobs[0].dynamic)

    def test_unquoted_float_like_version_note(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: [3.10, "3.11"]
"""})
        joined = " ".join(jobs[0].notes)
        self.assertIn("3.10", joined)
        self.assertNotIn("3.11", joined)

    def test_quoted_float_like_version_no_note(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10"]
"""})
        self.assertEqual(jobs[0].notes, [])

    def test_duplicate_values_note(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9", "3.9"]
"""})
        self.assertTrue(any("duplicate" in n for n in jobs[0].notes))

    def test_cap_truncates(self):
        jobs, _ = parse_workflow_file({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        a: [1, 2, 3]
        b: [4, 5, 6]
        c: [7, 8, 9]
"""}, cap=10)
        self.assertTrue(jobs[0].truncated)
        self.assertIsNone(jobs[0].combos)

    def test_other_keys_do_not_leak_into_matrix(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.9"]
      max-parallel: 2
"""})
        self.assertEqual(jobs[0].axes, {"python-version": ["3.9"]})


    def test_flow_exclude_one_line(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: windows-latest
    strategy:
      matrix:
        os: [windows-latest]
        python-version: ["3.9", "3.10"]
        exclude: [{os: windows-latest, python-version: "3.9"}]
"""})
        self.assertEqual(jobs[0].coverage()["python-version"], {"3.10"})

    def test_flow_include_one_line(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9"]
        include: [{python-version: "3.11"}]
"""})
        self.assertEqual(jobs[0].coverage()["python-version"], {"3.9", "3.11"})

    def test_include_only_matrix(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        include:
          - python-version: "3.9"
            os: ubuntu-latest
          - python-version: "3.10"
            os: windows-latest
"""})
        cov = jobs[0].coverage()
        self.assertEqual(cov["python-version"], {"3.9", "3.10"})
        self.assertEqual(cov["os"], {"ubuntu-latest", "windows-latest"})

    def test_indentless_block_sequence(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version:
        - "3.9"
        - "3.10"
"""})
        self.assertEqual(jobs[0].axes["python-version"], ["3.9", "3.10"])

    def test_multiline_flow_list(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: [
          "3.9",
          "3.10",
        ]
"""})
        self.assertEqual(jobs[0].axes["python-version"], ["3.9", "3.10"])

    def test_matrix_level_expression_is_dynamic(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}
"""})
        self.assertTrue(jobs[0].dynamic)
        self.assertEqual(jobs[0].axes, {})

    def test_matrix_flow_map(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix: {python-version: ["3.9"], os: ubuntu-latest}
"""})
        self.assertEqual(jobs[0].axes["python-version"], ["3.9"])
        self.assertEqual(jobs[0].axes["os"], ["ubuntu-latest"])

    def test_matrix_anchor_is_parsed_and_alias_is_dynamic(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  linux:
    runs-on: ubuntu-latest
    strategy:
      matrix: &m
        python-version: ["3.9"]
  copy:
    runs-on: ubuntu-latest
    strategy:
      matrix: *m
"""})
        self.assertEqual(jobs[0].axes["python-version"], ["3.9"])
        self.assertEqual(jobs[1].axes, {})
        self.assertTrue(jobs[1].dynamic)

    def test_runner_axis_counts_as_os(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ${{ matrix.runner }}
    strategy:
      matrix:
        runner: [windows-latest, ubuntu-latest]
        python-version: ["3.9"]
"""})
        self.assertEqual(jobs[0].coverage()["runner"], {"windows-latest", "ubuntu-latest"})


    def test_runs_on_indentless_block_list(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on:
    - ubuntu-latest
    - windows-latest
"""})
        self.assertEqual(jobs[0].runs_on, ["ubuntu-latest", "windows-latest"])

    def test_runs_on_flow_map_labels(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: {group: my-group, labels: ubuntu-latest}
"""})
        self.assertEqual(jobs[0].runs_on, ["ubuntu-latest"])

    def test_reusable_workflow_input_is_not_coverage(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
  call:
    uses: org/ci/.github/workflows/t.yml@main
    with:
      python-version: "3.8"
"""})
        self.assertFalse(any(j.axes.get("python-version") == ["3.8"] for j in jobs))

    def test_symbolic_version_marks_dynamic(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.x"]
"""})
        self.assertTrue(jobs[0].dynamic)


    def test_include_applies_to_all_matching_combos(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        python-version: ["3.9", "3.10"]
        os: [ubuntu-latest, windows-latest]
        include:
          - os: windows-latest
            experimental: true
"""})
        self.assertEqual(len(jobs[0].combos), 4)
        windows = [c for c in jobs[0].combos if c["os"] == "windows-latest"]
        self.assertEqual(len(windows), 2)
        self.assertTrue(all(c.get("experimental") == "true" for c in windows))

    def test_anchored_include_is_dynamic(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9"]
        include: *inc
"""})
        self.assertTrue(jobs[0].dynamic)

    def test_next_line_flow_axis(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version:
          ["3.9", "3.10"]
"""})
        self.assertEqual(jobs[0].axes["python-version"], ["3.9", "3.10"])

    def test_next_line_flow_matrix(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        {python-version: ["3.9"], os: ubuntu-latest}
"""})
        self.assertEqual(jobs[0].axes["python-version"], ["3.9"])
        self.assertEqual(jobs[0].axes["os"], ["ubuntu-latest"])

    def test_multiline_flow_include_map(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        python-version: ["3.9"]
        include:
          - {os: windows-latest,
             python-version: "3.10"}
"""})
        cov = jobs[0].coverage()
        self.assertEqual(cov["os"], {"windows-latest"})
        self.assertEqual(cov["python-version"], {"3.9", "3.10"})


    def test_with_before_uses(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - with:
          python-version: "3.10"
        uses: actions/setup-python@v5
"""})
        self.assertTrue(any(j.axes.get("python-version") == ["3.10"] for j in jobs))

    def test_flow_style_with(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with: {python-version: "3.10"}
"""})
        self.assertTrue(any(j.axes.get("python-version") == ["3.10"] for j in jobs))

    def test_dynamic_scalar_marks_dynamic(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ env.PY }}
"""})
        self.assertTrue(any(j.dynamic for j in jobs))

    def test_runs_on_multiline_flow_list(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: [
      ubuntu-latest,
      windows-latest
    ]
"""})
        self.assertEqual(jobs[0].runs_on, ["ubuntu-latest", "windows-latest"])

    def test_rust_toolchain_action_ref(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: dtolnay/rust-toolchain@1.70.0
"""})
        self.assertTrue(any(j.axes.get("toolchain") == ["1.70.0"] for j in jobs))


class WorkflowReaderTests(unittest.TestCase):
    def test_runs_on_block_list(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on:
      - ubuntu-latest
      - windows-latest
"""})
        self.assertEqual(jobs[0].runs_on, ["ubuntu-latest", "windows-latest"])

    def test_matrix_os(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
"""})
        self.assertEqual(jobs[0].axes["os"], ["ubuntu-latest", "macos-latest"])

    def test_scalar_setup_version_under_with(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
"""})
        self.assertTrue(any(j.axes.get("python-version") == ["3.12"] for j in jobs))

    def test_scalar_outside_with_ignored(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: echo "python-version: 3.12"
"""})
        self.assertFalse(any(j.axes.get("python-version") == ["3.12"] for j in jobs))

    def test_block_scalar_body_ignored(self):
        jobs, _ = job({".github/workflows/ci.yml": """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: |
          matrix:
            python-version: ["3.9"]
"""})
        self.assertEqual(jobs[0].axes, {})

    def test_crlf(self):
        content = "jobs:\r\n  test:\r\n    runs-on: ubuntu-latest\r\n    strategy:\r\n      matrix:\r\n        python-version: [\"3.9\"]\r\n"
        jobs, _ = job({".github/workflows/ci.yml": content})
        self.assertEqual(jobs[0].axes["python-version"], ["3.9"])

    def test_tab_indent_warns(self):
        content = "jobs:\n\ttest:\n\t\truns-on: ubuntu-latest\n"
        _, report = job({".github/workflows/ci.yml": content})
        self.assertTrue(any("tab indentation" in w for w in report.warnings))

    def test_no_jobs_block(self):
        jobs, _ = job({".github/workflows/ci.yml": "name: ci\non: push\n"})
        self.assertEqual(jobs, [])

    def test_multiple_workflows(self):
        root = make_repo({
            ".github/workflows/a.yml": "jobs:\n  a:\n    runs-on: ubuntu-latest\n",
            ".github/workflows/b.yaml": "jobs:\n  b:\n    runs-on: windows-latest\n",
        })
        cleanup(self, root)
        report = mg.Report(root=str(root))
        jobs = []
        for p in sorted((root / ".github" / "workflows").iterdir()):
            jobs.extend(mg.parse_workflow(p, root, report, 5000))
        self.assertEqual({j.job for j in jobs}, {"a", "b"})
        self.assertEqual({j.workflow for j in jobs},
                         {".github/workflows/a.yml", ".github/workflows/b.yaml"})


if __name__ == "__main__":
    unittest.main()
