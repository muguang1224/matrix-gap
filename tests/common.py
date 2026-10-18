"""Shared helpers for the matrix-gap test suite."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import matrix_gap as mg  # noqa: E402


def make_repo(files):
    """Create a temporary directory populated with {relative_path: content}."""
    root = Path(tempfile.mkdtemp(prefix="matrix-gap-test-"))
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return root


def cleanup(testcase, root):
    testcase.addCleanup(shutil.rmtree, str(root), True)


def run_main(*args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = mg.main([str(a) for a in args])
    return code, buf.getvalue()


def run_json(*args):
    import json
    code, out = run_main(*[str(a) for a in args] + ["--json"])
    return code, json.loads(out)


def parse_workflow_file(files, name=".github/workflows/ci.yml", cap=5000):
    root = make_repo(files)
    report = mg.Report(root=str(root))
    jobs = mg.parse_workflow(root / name, root, report, cap)
    return jobs, report


def analyze(files, **kwargs):
    root = make_repo(files)
    return mg.analyze(root, **kwargs)
