#!/usr/bin/env python3
"""matrix-gap -- audit claimed platform support against what CI actually tests.

Zero dependencies. Reads a repository, extracts machine-readable support
claims (Python / Node / Go / Rust versions and operating systems) and the
GitHub Actions build matrix, expands the matrix (include / exclude), and
reports the gaps between "we say we support it" and "we test it".

Exit codes:
  0  no findings at or above --fail-on
  1  findings at or above --fail-on
  2  tool error (bad path, unreadable input, invalid arguments)

Design boundary: this is a static reader. It never edits files, never talks
to the network (end-of-life data, when enabled, is bundled or supplied by the
caller), and refuses to guess when a matrix is dynamic.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from itertools import product
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

__version__ = "0.1.0"
SCHEMA_VERSION = 1

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}

# Best-effort release windows. Only used with --eol. Kept conservative: a
# version is only considered EOL when the date is in the past.
EOL_PYTHON = {
    "3.7": "2023-06-27",
    "3.8": "2024-10-07",
    "3.9": "2025-10-31",
    "3.10": "2026-10-04",
    "3.11": "2027-10-24",
    "3.12": "2028-10-24",
    "3.13": "2029-10-24",
    "3.14": "2030-10-24",
    "3.15": "2031-10-24",
}
EOL_NODE = {
    "14": "2023-04-30",
    "16": "2023-09-11",
    "18": "2025-04-30",
    "20": "2026-04-30",
    "22": "2027-04-30",
    "24": "2028-04-30",
    "26": "2029-04-30",
}

# Candidates used to expand ">=" style claims. Newer releases are unknown to
# this snapshot on purpose: the tool must not invent support for versions it
# has never seen.
PY_CANDIDATES = tuple("3.%d" % m for m in range(7, 16))        # 3.7 .. 3.15
NODE_CANDIDATES = tuple(str(m) for m in range(14, 27))         # 14 .. 26
NODE_LTS = {"14", "16", "18", "20", "22", "24", "26"}          # even majors
GO_CANDIDATES = tuple("1.%d" % m for m in range(19, 28))       # 1.19 .. 1.27

PY_KEYS = {"python", "python-version", "python_version", "py"}
NODE_KEYS = {"node", "node-version", "node_version"}
GO_KEYS = {"go", "go-version", "go_version"}
RUST_KEYS = {"rust", "rust-version", "rust_version", "toolchain"}
OS_KEYS = {"os", "platform", "runner", "vm", "machine"}

OS_CLASSIFIERS = {
    "Operating System :: Microsoft :: Windows": "windows",
    "Operating System :: POSIX :: Linux": "linux",
    "Operating System :: MacOS": "macos",
    "Operating System :: MacOS :: MacOS X": "macos",
}


class ToolError(Exception):
    """Fatal, user-facing error."""


class TomlError(Exception):
    pass


class DynamicSpec(Exception):
    pass


# ---------------------------------------------------------------------------
# small utilities


def read_text(path: Path) -> str:
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise ValueError("contains NUL bytes")
    return data.decode("utf-8-sig")


def rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def find_line(text: str, needle: str, start: int = 1) -> int:
    """1-based line number of the first line containing needle."""
    for i, line in enumerate(text.splitlines(), 1):
        if i >= start and needle in line:
            return i
    return start


def _split_top_level(s: str, sep: str = ",") -> List[str]:
    parts: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    escape = False
    depth = 0
    for ch in s:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"':
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch in "[{(":
            depth += 1
            buf.append(ch)
            continue
        if ch in "]})":
            depth -= 1
            buf.append(ch)
            continue
        if ch == sep and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf or s.endswith(sep):
        parts.append("".join(buf).strip())
    return parts


def _strip_quotes(s: str) -> Tuple[str, bool]:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1], True
    return s, False


# ---------------------------------------------------------------------------
# TOML subset parser (fallback for Python < 3.11). Tolerant: values we do not
# understand are kept as raw strings instead of failing the whole file.


def _strip_toml_comment(line: str) -> str:
    out: List[str] = []
    quote: Optional[str] = None
    escape = False
    for ch in line:
        if escape:
            out.append(ch)
            escape = False
            continue
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"':
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def _brackets_closed(s: str) -> bool:
    depth = 0
    quote: Optional[str] = None
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if quote:
            if ch == "\\" and quote == '"':
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
    return depth <= 0 and quote is None


def _parse_toml_string(v: str) -> str:
    q = v[0]
    body = v[1:]
    if body.endswith(q):
        body = body[:-1]
    if q == "'":
        return body
    out: List[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "b": "\b", "f": "\f"}
            if nxt in mapping:
                out.append(mapping[nxt])
                i += 2
                continue
            if nxt == "u" and i + 5 < len(body):
                try:
                    out.append(chr(int(body[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_toml_value(v: str) -> Any:
    v = v.strip()
    if not v:
        return ""
    if v.startswith("["):
        inner = v[1:]
        if inner.endswith("]"):
            inner = inner[:-1]
        return [_parse_toml_value(p) for p in _split_top_level(inner) if p != ""]
    if v[0] in "\"'":
        return _parse_toml_string(v)
    if v in ("true", "false"):
        return v == "true"
    if re.fullmatch(r"[+-]?\d+", v):
        return int(v)
    if re.fullmatch(r"[+-]?\d+\.\d+", v):
        return float(v)
    if v.startswith("{") and v.endswith("}"):
        out: Dict[str, Any] = {}
        for part in _split_top_level(v[1:-1]):
            if "=" in part:
                k, val = part.split("=", 1)
                out[_strip_quotes(k)[0]] = _parse_toml_value(val)
        return out
    return v


def _toml_set(table: Dict[str, Any], key: str, value: Any) -> None:
    parts = [_strip_quotes(p)[0] for p in _split_top_level(key, ".")]
    target = table
    for part in parts[:-1]:
        nxt = target.setdefault(part, {})
        if not isinstance(nxt, dict):
            nxt = {}
            target[part] = nxt
        target = nxt
    target[parts[-1]] = value


def parse_toml(text: str) -> Dict[str, Any]:
    """Parse the subset of TOML used by pyproject.toml / Cargo.toml."""
    root: Dict[str, Any] = {}
    current = root
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line_no = i + 1
        s = _strip_toml_comment(raw).strip()
        if not s:
            i += 1
            continue
        if s.startswith("[["):
            if not s.endswith("]]"):
                raise TomlError("line %d: malformed array-of-tables header" % line_no)
            parts = [_strip_quotes(p)[0] for p in _split_top_level(s[2:-2].strip(), ".")]
            if not parts:
                raise TomlError("line %d: empty table name" % line_no)
            parent = root
            for part in parts[:-1]:
                nxt = parent.setdefault(part, {})
                if not isinstance(nxt, dict):
                    nxt = {}
                    parent[part] = nxt
                parent = nxt
            bucket = parent.setdefault(parts[-1], [])
            if not isinstance(bucket, list):
                bucket = []
                parent[parts[-1]] = bucket
            current = {}
            bucket.append(current)
            i += 1
            continue
        if s.startswith("["):
            if not s.endswith("]"):
                raise TomlError("line %d: malformed table header" % line_no)
            name = s[1:-1].strip()
            current = root
            for part in _split_top_level(name, "."):
                part = _strip_quotes(part)[0]
                nxt = current.setdefault(part, {})
                if not isinstance(nxt, dict):
                    nxt = {}
                    current[part] = nxt
                current = nxt
            i += 1
            continue
        if "=" not in s:
            raise TomlError("line %d: expected key = value" % line_no)
        key, val = s.split("=", 1)
        key = _strip_quotes(key.strip())[0]
        val = val.strip()
        if val[:3] in ('"""', "'''"):
            q = val[:3]
            buf = val[3:]
            while q not in buf:
                i += 1
                if i >= len(lines):
                    raise TomlError("line %d: unterminated multi-line string" % line_no)
                buf += "\n" + lines[i]
            buf = buf.split(q, 1)[0]
            _toml_set(current, key, buf)
            i += 1
            continue
        if val.startswith("[") and not _brackets_closed(val):
            buf = val
            while not _brackets_closed(buf):
                i += 1
                if i >= len(lines):
                    raise TomlError("line %d: unterminated array" % line_no)
                buf += "\n" + _strip_toml_comment(lines[i])
            val = buf
        _toml_set(current, key, _parse_toml_value(val))
        i += 1
    return root


def load_toml(path: Path, prefer_stdlib: bool = True) -> Dict[str, Any]:
    text = read_text(path)
    if prefer_stdlib:
        try:
            import tomllib  # type: ignore
        except Exception:
            tomllib = None  # type: ignore
        if tomllib is not None:
            return tomllib.loads(text)
    return parse_toml(text)


# ---------------------------------------------------------------------------
# version ranges


def _py_tuple(v: str) -> Tuple[int, int, int]:
    m = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.\d+)*"
                 r"(?:[.-]?[a-zA-Z][a-zA-Z0-9.]*)?$", v.strip())
    if not m:
        raise ValueError("not a Python version: %r" % v)
    return tuple(int(x or 0) for x in m.groups())  # type: ignore


PY_TUPLES = tuple((_py_tuple(v), v) for v in PY_CANDIDATES)


def python_spec_versions(spec: str, candidates: Sequence[str] = PY_CANDIDATES) -> List[str]:
    """Expand a PEP 440 subset (>=, >, <=, <, ==, !=, ~=, wildcards)."""
    spec = spec.strip()
    if not spec or spec == "*":
        return list(candidates)
    clauses = [c.strip() for c in spec.split(",") if c.strip()]
    parsed = []
    for clause in clauses:
        m = re.match(r"^(>=|<=|==|!=|~=|<|>)?\s*([0-9][0-9A-Za-z.*+!-]*)$", clause)
        if not m:
            raise ValueError("unsupported version clause: %r" % clause)
        op = m.group(1) or "=="
        ver = m.group(2)
        parsed.append((op, ver))

    out: List[str] = []
    for cand_s in candidates:
        cand = _py_tuple(cand_s)
        ok = True
        for op, ver in parsed:
            wildcard = ver.endswith(".*")
            ver_clean = ver[:-2] if wildcard else ver
            try:
                target = _py_tuple(ver_clean)
            except ValueError:
                raise ValueError("unsupported version operand: %r" % ver)
            if wildcard:
                if op in ("==", "!="):
                    components = len(ver_clean.split("."))
                    same = cand[:components] == target[:components]
                    ok = same if op == "==" else not same
                    continue
                ok = False
                break
            has_patch = ver_clean.count(".") >= 2
            c = cand[:2]
            t = target[:2]
            if op == ">=":
                ok = c >= t
            elif op == ">":
                # Candidates are minor lines; a patch bound still leaves
                # satisfying patches inside the same minor line.
                ok = c >= t if has_patch else c > t
            elif op == "<=":
                ok = c <= t
            elif op == "<":
                ok = c <= t if has_patch else c < t
            elif op in ("==", "==="):
                ok = c == t
            elif op == "!=":
                # !=3.9.0 does not exclude the whole 3.9 line.
                ok = True if (has_patch and not wildcard) else c != t
            elif op == "~=":
                if ver_clean.count(".") == 1:
                    ok = c >= t and cand[0] == target[0]
                else:
                    ok = c == t
            if not ok:
                break
        if ok:
            out.append(cand_s)
    return out


def _spec_allows(spec: str, version: str) -> Optional[bool]:
    try:
        return bool(python_spec_versions(spec, [version]))
    except ValueError:
        return None


def _node_major(v: str) -> Optional[int]:
    m = re.match(r"^v?(\d+)", v.strip())
    return int(m.group(1)) if m else None


def _node_clause(cand: int, clause: str) -> bool:
    clause = clause.strip()
    if clause in ("*", "x", "X"):
        return True
    if clause.lower() in ("lts/*", "lts", "latest", "current", "node"):
        raise DynamicSpec(clause)
    hyphen = re.match(r"^v?(\d+)\s*-\s*v?(\d+)$", clause)
    if hyphen:
        return int(hyphen.group(1)) <= cand <= int(hyphen.group(2))
    m = re.match(r"^(\^|~|>=|<=|>|<|=)?\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(?:x|X|\*))?$", clause)
    if not m:
        raise DynamicSpec(clause)
    op = m.group(1) or "="
    major = int(m.group(2))
    if op == "=":
        return cand == major
    if op == ">=":
        return cand >= major
    if op == ">":
        return cand > major
    if op == "<=":
        return cand <= major
    if op == "<":
        return cand < major
    if op == "^":
        return cand >= major and cand < major + 1
    if op == "~":
        return cand == major
    return False


def node_spec_versions(spec: str, candidates: Sequence[str] = NODE_CANDIDATES) -> List[str]:
    spec = spec.strip()
    if not spec:
        return list(candidates)
    # Normalise ">= 18" and "18 - 20" into tokens the clause parser accepts.
    spec = re.sub(r"(>=|<=|>|<|\^|~|=)\s+", r"\1", spec)
    spec = re.sub(r"(\d)\s*-\s*(v?\d)", r"\1-\2", spec)
    alternatives = [a.strip() for a in spec.split("||") if a.strip()]
    out: List[str] = []
    for cand_s in candidates:
        major = int(cand_s)
        try:
            if any(all(_node_clause(major, c) for c in alt.split()) for alt in alternatives):
                out.append(cand_s)
        except DynamicSpec:
            raise
        except Exception:
            raise DynamicSpec(spec)
    return out


def normalize_python(v: str) -> Optional[str]:
    v = v.strip().strip("\"'")
    if v.lower() in ("3.x", "3", "python3", "py3"):
        return None
    m = re.match(r"^3\.(\d+)", v)
    return "3.%s" % m.group(1) if m else None


def normalize_node(v: str) -> Optional[str]:
    v = v.strip().strip("\"'")
    if v.lower() in ("lts/*", "lts", "latest", "current", "node", "stable"):
        return None
    m = re.match(r"^v?(\d+)", v)
    return m.group(1) if m else None


def normalize_go(v: str) -> Optional[str]:
    v = v.strip().strip("\"'")
    if v.lower() in ("stable", "oldstable", "latest"):
        return None
    m = re.match(r"^(?:go\s*)?1\.(\d+)", v)
    return "1.%s" % m.group(1) if m else None


def normalize_rust(v: str) -> Optional[str]:
    v = v.strip().strip("\"'")
    if v.lower() in ("stable", "beta", "nightly", "msrv"):
        return None
    m = re.match(r"^(\d+)\.(\d+)", v)
    return "%s.%s" % (m.group(1), m.group(2)) if m else None


# ---------------------------------------------------------------------------
# data model


@dataclass
class Claim:
    ecosystem: str
    kind: str
    value: str
    source: str
    line: int
    versions: List[str] = field(default_factory=list)


@dataclass
class CiJob:
    workflow: str
    job: str
    line: int
    axes: Dict[str, List[str]] = field(default_factory=dict)
    combos: Optional[List[Dict[str, str]]] = None
    runs_on: List[str] = field(default_factory=list)
    dynamic: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    truncated: bool = False

    def coverage(self) -> Dict[str, Set[str]]:
        out: Dict[str, Set[str]] = {k: set() for k in self.axes}
        if self.combos is None:
            for k, vals in self.axes.items():
                out[k] = set(vals)
            return out
        for combo in self.combos:
            for k, value in combo.items():
                out.setdefault(k, set()).add(str(value))
        return out


@dataclass
class Finding:
    code: str
    severity: str
    ecosystem: str
    message: str
    detail: str = ""
    source: str = ""
    line: int = 0


@dataclass
class Report:
    root: str
    claims: List[Claim] = field(default_factory=list)
    jobs: List[CiJob] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    coverage: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# claims


def _add_claim(report: Report, claim: Claim) -> None:
    key = (claim.ecosystem, claim.kind, claim.value, claim.source, claim.line)
    for existing in report.claims:
        if (existing.ecosystem, existing.kind, existing.value,
                existing.source, existing.line) == key:
            return
    report.claims.append(claim)


def _poetry_spec(value: str) -> str:
    value = value.strip()
    if value.startswith("^"):
        v = value[1:]
        try:
            major = _py_tuple(v)[0]
        except ValueError:
            return value
        return ">=%s,<%d" % (v, major + 1)
    if value.startswith("~"):
        v = value[1:]
        try:
            major, minor, _ = _py_tuple(v)
        except ValueError:
            return value
        return ">=%s,<%d.%d" % (v, major, minor + 1)
    return value


def parse_pyproject(path: Path, root: Path, report: Report) -> None:
    try:
        data = load_toml(path)
    except Exception as exc:
        report.warnings.append("%s: could not parse TOML (%s)" % (rel(path, root), exc))
        return
    text = read_text(path)
    project = data.get("project")
    if not isinstance(project, dict):
        project = {}
    req = project.get("requires-python")
    if isinstance(req, str) and req.strip():
        try:
            versions = python_spec_versions(req)
        except ValueError as exc:
            report.warnings.append("%s: requires-python not understood (%s)" % (rel(path, root), exc))
            versions = []
        _add_claim(report, Claim("python", "requires-python", req.strip(), rel(path, root),
                                 find_line(text, "requires-python"), versions))

    def add_classifiers(classifiers: Any) -> None:
        if not isinstance(classifiers, list):
            return
        for cl in classifiers:
            if not isinstance(cl, str):
                continue
            m = re.match(r"^Programming Language :: Python :: (3\.\d+)$", cl.strip())
            if m:
                _add_claim(report, Claim("python", "classifier", cl.strip(), rel(path, root),
                                         find_line(text, cl), [m.group(1)]))
            elif cl.strip() in OS_CLASSIFIERS:
                _add_claim(report, Claim("os", "classifier", OS_CLASSIFIERS[cl.strip()],
                                         rel(path, root), find_line(text, cl), []))

    add_classifiers(project.get("classifiers"))

    # Poetry's non-PEP-621 metadata is still common.
    tool = data.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    if isinstance(poetry, dict):
        if not isinstance(req, str) or not req.strip():
            poetry_python = poetry.get("python")
            if isinstance(poetry_python, str) and poetry_python.strip():
                spec = _poetry_spec(poetry_python)
                try:
                    versions = python_spec_versions(spec)
                except ValueError as exc:
                    report.warnings.append("%s: tool.poetry python not understood (%s)"
                                           % (rel(path, root), exc))
                    versions = []
                _add_claim(report, Claim("python", "requires-python", spec, rel(path, root),
                                         find_line(text, "python ="), versions))
        add_classifiers(poetry.get("classifiers"))


def parse_setup_cfg(path: Path, root: Path, report: Report) -> None:
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read_string(read_text(path), source=str(path))
    except Exception as exc:
        report.warnings.append("%s: could not parse INI (%s)" % (rel(path, root), exc))
        return
    text = read_text(path)
    req = cp.get("options", "python_requires", fallback=None)
    if req and req.strip():
        try:
            versions = python_spec_versions(req)
        except ValueError as exc:
            report.warnings.append("%s: python_requires not understood (%s)" % (rel(path, root), exc))
            versions = []
        _add_claim(report, Claim("python", "requires-python", req.strip(), rel(path, root),
                                 find_line(text, "python_requires"), versions))
    classifiers = cp.get("metadata", "classifiers", fallback="")
    for cl in classifiers.splitlines():
        cl = cl.strip()
        if not cl:
            continue
        m = re.match(r"^Programming Language :: Python :: (3\.\d+)$", cl)
        if m:
            _add_claim(report, Claim("python", "classifier", cl, rel(path, root),
                                     find_line(text, cl), [m.group(1)]))
        elif cl in OS_CLASSIFIERS:
            _add_claim(report, Claim("os", "classifier", OS_CLASSIFIERS[cl],
                                     rel(path, root), find_line(text, cl), []))


def parse_setup_py(path: Path, root: Path, report: Report) -> None:
    text = read_text(path)
    m = re.search(r"python_requires\s*=\s*['\"]([^'\"]+)['\"]", text)
    if not m:
        return
    req = m.group(1).strip()
    try:
        versions = python_spec_versions(req)
    except ValueError as exc:
        report.warnings.append("%s: python_requires not understood (%s)" % (rel(path, root), exc))
        versions = []
    _add_claim(report, Claim("python", "requires-python", req, rel(path, root),
                             find_line(text, "python_requires"), versions))


def parse_package_json(path: Path, root: Path, report: Report) -> None:
    try:
        data = json.loads(read_text(path))
    except Exception as exc:
        report.warnings.append("%s: could not parse JSON (%s)" % (rel(path, root), exc))
        return
    text = read_text(path)
    engines = data.get("engines") if isinstance(data, dict) else None
    if not isinstance(engines, dict):
        return
    node = engines.get("node")
    if isinstance(node, str) and node.strip():
        try:
            versions = node_spec_versions(node)
        except DynamicSpec as exc:
            report.warnings.append("%s: engines.node %r is dynamic (%s); not expanded"
                                   % (rel(path, root), node, exc))
            versions = []
        except Exception as exc:
            report.warnings.append("%s: engines.node not understood (%s)" % (rel(path, root), exc))
            versions = []
        _add_claim(report, Claim("node", "engines", node.strip(), rel(path, root),
                                 find_line(text, "engines"), versions))


def parse_cargo_toml(path: Path, root: Path, report: Report) -> None:
    try:
        data = load_toml(path)
    except Exception as exc:
        report.warnings.append("%s: could not parse TOML (%s)" % (rel(path, root), exc))
        return
    text = read_text(path)
    package = data.get("package")
    msrv = None
    if isinstance(package, dict):
        candidate = package.get("rust-version")
        if isinstance(candidate, str):
            msrv = candidate
    if msrv is None:
        workspace = data.get("workspace")
        if isinstance(workspace, dict):
            ws_package = workspace.get("package")
            if isinstance(ws_package, dict) and isinstance(ws_package.get("rust-version"), str):
                msrv = ws_package["rust-version"]
    if msrv and msrv.strip():
        norm = normalize_rust(msrv)
        if norm:
            _add_claim(report, Claim("rust", "msrv", msrv.strip(), rel(path, root),
                                     find_line(text, "rust-version"), [norm]))


def parse_go_mod(path: Path, root: Path, report: Report) -> None:
    text = read_text(path)
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^go\s+(\d+\.\d+(?:\.\d+)?)\s*(?://.*)?$", line.strip())
        if m:
            norm = normalize_go(m.group(1))
            if norm:
                _add_claim(report, Claim("go", "go-directive", m.group(1), rel(path, root),
                                         i, [norm]))
            return


def _expand_tox_braces(token: str) -> List[str]:
    out = [token]
    changed = True
    while changed:
        changed = False
        for s in out:
            m = re.search(r"\{([^{}]+)\}", s)
            if not m:
                continue
            parts: List[str] = []
            for piece in m.group(1).split(","):
                rng = re.match(r"^(\d+)\s*-\s*(\d+)$", piece.strip())
                if rng:
                    start, end = int(rng.group(1)), int(rng.group(2))
                    if 0 <= end - start <= 50:
                        parts.extend(str(x) for x in range(start, end + 1))
                elif piece.strip():
                    parts.append(piece.strip())
            if parts:
                out = [s[:m.start()] + p + s[m.end():] for s in out for p in parts]
                changed = True
                break
    return out


def _split_tox_envlist(value: str) -> List[str]:
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    for ch in value:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch in ", \t\r\n" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def parse_tox(path: Path, root: Path, report: Report) -> None:
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read_string(read_text(path), source=str(path))
    except Exception as exc:
        report.warnings.append("%s: could not parse tox.ini (%s)" % (rel(path, root), exc))
        return
    envlist = re.sub(r"#.*", "", cp.get("tox", "envlist", fallback=""))
    text = read_text(path)
    versions: List[str] = []
    seen: Set[str] = set()
    for raw_token in _split_tox_envlist(envlist):
        raw_token = raw_token.strip()
        if not raw_token:
            continue
        for token in _expand_tox_braces(raw_token):
            m = re.match(r"^py3\.?(\d{1,2})(?:[-_].*)?$", token)
            if not m:
                m = re.match(r"^3\.(\d{1,2})$", token)
            if not m:
                m = re.match(r"^3(\d{1,2})$", token)
            if m:
                v = "3.%s" % m.group(1)
                if v not in seen:
                    seen.add(v)
                    versions.append(v)
    if versions:
        # tox is evidence of testing, not a support claim.
        report.jobs.append(CiJob(workflow=rel(path, root), job="<tox>",
                                 line=find_line(text, "envlist"),
                                 axes={"python-version": versions},
                                 combos=[{"python-version": v} for v in versions]))


# ---------------------------------------------------------------------------
# GitHub Actions workflow reader (targeted YAML subset)


def _indent_of(line: str) -> Tuple[int, bool]:
    n = 0
    tab = False
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 8
            tab = True
        else:
            break
    return n, tab


def _strip_yaml_comment(s: str) -> str:
    out: List[str] = []
    quote: Optional[str] = None
    escape = False
    for ch in s:
        if escape:
            out.append(ch)
            escape = False
            continue
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"':
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def _flow_items(s: str, opener: str, closer: str) -> List[str]:
    s = s.strip()
    if s.startswith(opener) and s.endswith(closer):
        s = s[1:-1]
    return [p for p in _split_top_level(s) if p != ""]


def _flow_map(s: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in _flow_items(s, "{", "}"):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        out[_strip_quotes(k)[0]] = _strip_quotes(v)[0]
    return out


def _flow_list_raw(s: str) -> List[Tuple[str, bool]]:
    return [_strip_quotes(p) for p in _flow_items(s, "[", "]")]


def _flow_list(s: str) -> List[str]:
    return [v for v, _ in _flow_list_raw(s)]


def _float_risk(value: str, quoted: bool, family: Optional[str]) -> bool:
    if quoted or family not in ("python", "node", "go"):
        return False
    return bool(re.fullmatch(r"\d+\.\d0", value))


def _family_of(key: str) -> Optional[str]:
    k = key.lower()
    if k in PY_KEYS:
        return "python"
    if k in NODE_KEYS:
        return "node"
    if k in GO_KEYS:
        return "go"
    if k in RUST_KEYS:
        return "rust"
    if k in OS_KEYS:
        return "os"
    return None


def _workflow_lines(path: Path) -> Tuple[List[str], List[int], List[bool]]:
    raw = read_text(path).splitlines()
    clean: List[str] = []
    indents: List[int] = []
    skip: List[bool] = []
    tab_warn = False
    for line in raw:
        ind, tab = _indent_of(line)
        tab_warn = tab_warn or tab
        clean.append(_strip_yaml_comment(line).strip())
        indents.append(ind)
        skip.append(False)
    # Skip block scalars so `run: |` bodies cannot masquerade as config.
    for i, line in enumerate(clean):
        if not line:
            continue
        if re.search(r":\s*[|>][+-]?\s*$", line) or re.match(r"^\s*-\s*[|>][+-]?\s*$", line):
            base = indents[i]
            j = i + 1
            while j < len(clean):
                if clean[j].strip() and indents[j] <= base:
                    break
                skip[j] = True
                j += 1
    if tab_warn:
        # No global place to put warnings here; caller can inspect.
        pass
    return clean, indents, skip


def _parse_block_list(items: List[Tuple[int, int, str]], i: int,
                      parent: int) -> Tuple[List[Tuple[str, bool]], int]:
    out: List[Tuple[str, bool]] = []
    item_indent: Optional[int] = None
    while i < len(items):
        _, ind, s = items[i]
        if ind < parent:
            break
        if ind == parent and not s.startswith("-"):
            break
        if s.startswith("-"):
            if item_indent is None:
                item_indent = ind
            if ind == item_indent:
                value, quoted = _strip_quotes(s[1:].strip())
                if value and not value.startswith("{"):
                    out.append((value, quoted))
        i += 1
    return out, i


def _parse_map_list(items: List[Tuple[int, int, str]], i: int, parent: int) -> Tuple[List[Dict[str, str]], int]:
    out: List[Dict[str, str]] = []
    item_indent: Optional[int] = None
    current: Optional[Dict[str, str]] = None
    while i < len(items):
        _, ind, s = items[i]
        if ind < parent:
            break
        if ind == parent and not s.startswith("-"):
            break
        if s.startswith("-"):
            if item_indent is None:
                item_indent = ind
            if ind == item_indent:
                if current is not None:
                    out.append(current)
                current = {}
                rest = s[1:].strip()
                if rest.startswith("{"):
                    full, j = _collect_flow(items, i, rest)
                    current.update(_flow_map(full))
                    i = j
                elif ":" in rest:
                    k, v = rest.split(":", 1)
                    current[_strip_quotes(k)[0]] = _strip_quotes(v)[0]
        elif current is not None and ":" in s:
            k, v = s.split(":", 1)
            current[_strip_quotes(k.strip())[0]] = _strip_quotes(v)[0]
        i += 1
    if current is not None:
        out.append(current)
    return out, i


def _collect_flow(items: List[Tuple[int, int, str]], i: int, text: str) -> Tuple[str, int]:
    """Join a flow collection that continues on following lines."""
    while not _brackets_closed(text):
        i += 1
        if i >= len(items):
            break
        text += " " + items[i][2]
    return text, i


def _peek_flow(items: List[Tuple[int, int, str]], i: int,
               parent: int) -> Tuple[Optional[str], int]:
    """Return a flow collection that starts on the line after a key."""
    j = i + 1
    while j < len(items):
        _, ind, s = items[j]
        if ind <= parent:
            return None, i + 1
        if s:
            if s.startswith("[") or s.startswith("{"):
                return s, j
            return None, i + 1
        j += 1
    return None, i + 1


def _parse_matrix(items: List[Tuple[int, int, str]], start: int, parent: int,
                  notes: List[str], dynamic: List[str]) -> Dict[str, Any]:
    axes: Dict[str, List[str]] = {}
    include: List[Dict[str, str]] = []
    exclude: List[Dict[str, str]] = []
    local = [it for it in items if it[0] >= start]
    # Find the axis indentation: the smallest indent greater than `parent`.
    candidates = [ind for _, ind, s in local if s and ind > parent]
    if not candidates:
        return {"axes": axes, "include": include, "exclude": exclude}
    base = min(candidates)
    i = 0
    while i < len(local):
        _, ind, s = local[i]
        if ind != base or not s or s.startswith("-"):
            i += 1
            continue
        m = re.match(r"^([^:]+):\s*(.*)$", s)
        if not m:
            i += 1
            continue
        key = _strip_quotes(m.group(1).strip())[0]
        rest = m.group(2).strip()
        if key in ("include", "exclude"):
            parsed: List[Dict[str, str]] = []
            if rest.startswith("&"):
                parsed, i = _parse_map_list(local, i + 1, ind)
                notes.append("%s uses an anchor; aliases elsewhere are not resolved" % key)
            elif rest.startswith("*") or "${{" in rest or "fromJSON" in rest:
                dynamic.append("%s: %s" % (key, rest))
                i += 1
            elif rest.startswith("["):
                full, last = _collect_flow(local, i, rest)
                parsed = [_flow_map(part) for part in _flow_items(full, "[", "]")
                          if part.strip().startswith("{")]
                i = last + 1
            elif rest.startswith("{"):
                parsed = [_flow_map(rest)]
                i += 1
            else:
                peek, j = _peek_flow(local, i, ind)
                if peek is not None:
                    full, last = _collect_flow(local, j, peek)
                    parsed = [_flow_map(part) for part in _flow_items(full, "[", "]")
                              if part.strip().startswith("{")]
                    i = last + 1
                else:
                    parsed, i = _parse_map_list(local, i + 1, ind)
            if key == "include":
                include = parsed
            else:
                exclude = parsed
            continue
        family = _family_of(key)
        if rest:
            if rest.startswith("["):
                full, last = _collect_flow(local, i, rest)
                raw_values = _flow_list_raw(full)
                axes[key] = [v for v, _ in raw_values]
                for value, quoted in raw_values:
                    if _float_risk(value, quoted, family):
                        notes.append("unquoted %r may be read as a float by YAML; quote it" % value)
                i = last + 1
                continue
            if "${{" in rest or "fromJSON" in rest:
                dynamic.append("%s: %s" % (key, rest))
                axes[key] = []
            elif rest.startswith("&") or rest.startswith("*"):
                dynamic.append("%s: %s (anchor/alias is not resolved)" % (key, rest))
                axes[key] = []
            elif rest.startswith("{"):
                dynamic.append("%s: %s (flow map is not a version list)" % (key, rest))
                axes[key] = []
            else:
                value, quoted = _strip_quotes(rest)
                axes[key] = [value]
                if _float_risk(value, quoted, family):
                    notes.append("unquoted %r may be read as a float by YAML; quote it" % value)
            i += 1
            continue
        peek, j = _peek_flow(local, i, ind)
        if peek is not None:
            full, last = _collect_flow(local, j, peek)
            if full.startswith("["):
                raw_values = _flow_list_raw(full)
                axes[key] = [v for v, _ in raw_values]
                for value, quoted in raw_values:
                    if _float_risk(value, quoted, family):
                        notes.append("unquoted %r may be read as a float by YAML; quote it" % value)
            else:
                dynamic.append("%s: %s (flow map is not a version list)" % (key, peek))
                axes[key] = []
            i = last + 1
            continue
        raw_values, i = _parse_block_list(local, i + 1, ind)
        axes[key] = [v for v, _ in raw_values]
        for value, quoted in raw_values:
            if _float_risk(value, quoted, family):
                notes.append("unquoted %r may be read as a float by YAML; quote it" % value)
    return {"axes": axes, "include": include, "exclude": exclude}


def _expand_matrix(axes: Dict[str, List[str]], include: List[Dict[str, str]],
                   exclude: List[Dict[str, str]], cap: int) -> Tuple[Optional[List[Dict[str, str]]], bool]:
    keys = [k for k, vals in axes.items() if vals]
    if not keys:
        combos: List[Dict[str, str]] = [{}]
    else:
        total = 1
        for k in keys:
            total *= len(axes[k])
        if total > cap:
            return None, True
        combos = [dict(zip(keys, values)) for values in product(*(axes[k] for k in keys))]
    for exc in exclude:
        combos = [c for c in combos
                  if not all(k in c and str(c[k]) == str(v) for k, v in exc.items())]
    for inc in include:
        targets = [combo for combo in combos
                   if all(k not in combo or str(combo[k]) == str(v) for k, v in inc.items())]
        if targets:
            for combo in targets:
                combo.update({k: v for k, v in inc.items()})
        else:
            combos.append(dict(inc))
        if len(combos) > cap:
            return None, True
    return combos, False


def _step_actions(clean: List[str], indents: List[int], index: int) -> List[str]:
    """Return the `uses:` refs of the step containing line `index`."""
    start = None
    base = None
    for j in range(index, -1, -1):
        if clean[j].startswith("-") and indents[j] < indents[index]:
            start = j
            base = indents[j]
            break
    if start is None:
        return []
    actions: List[str] = []
    for k in range(start, len(clean)):
        if k > start and clean[k].startswith("-") and indents[k] <= base:
            break
        if k > start and clean[k] and indents[k] < base:
            break
        if clean[k].startswith("- uses:") or clean[k].startswith("uses:"):
            actions.append(clean[k].split("uses:", 1)[1].strip())
    return actions


def _step_uses_action(clean: List[str], indents: List[int], index: int, action: str) -> bool:
    return any(action in ref for ref in _step_actions(clean, indents, index))


def _under_key(clean: List[str], indents: List[int], index: int, key: str) -> bool:
    """True when line `index` is nested under a `key:` line."""
    ind = indents[index]
    for j in range(index - 1, -1, -1):
        if not clean[j]:
            continue
        if indents[j] < ind:
            return clean[j] == key + ":" or clean[j] == "- " + key + ":"
    return False


def _apply_flow_matrix(job: CiJob, text: str, cap: int) -> None:
    for key, raw_value in _flow_map(text).items():
        if key in ("include", "exclude"):
            job.dynamic.append("flow-style %s is not expanded" % key)
        elif raw_value.startswith("["):
            raw_values = _flow_list_raw(raw_value)
            job.axes[key] = [v for v, _ in raw_values]
            for value, quoted in raw_values:
                if _float_risk(value, quoted, _family_of(key)):
                    job.notes.append("unquoted %r may be read as a float by YAML; quote it" % value)
        elif "${{" in raw_value:
            job.dynamic.append("%s: %s" % (key, raw_value))
            job.axes[key] = []
        else:
            job.axes[key] = [_strip_quotes(raw_value)[0]]
    job.combos, job.truncated = _expand_matrix(job.axes, [], [], cap)


def parse_workflow(path: Path, root: Path, report: Report, cap: int) -> List[CiJob]:
    relpath = rel(path, root)
    try:
        raw_text = read_text(path)
        if re.search(r"(?m)^\t+\S", raw_text):
            report.warnings.append("%s: tab indentation is not valid YAML" % relpath)
        clean, indents, skip = _workflow_lines(path)
    except Exception as exc:
        report.warnings.append("%s: could not read workflow (%s)" % (relpath, exc))
        return []
    lines = [(i, indents[i], clean[i]) for i in range(len(clean))]
    jobs: List[CiJob] = []
    jobs_idx = None
    for i, s in enumerate(clean):
        if indents[i] == 0 and re.match(r"^jobs:\s*$", s):
            jobs_idx = i
            break
    if jobs_idx is None:
        return []
    end = len(clean)
    for j in range(jobs_idx + 1, len(clean)):
        if clean[j].strip() and indents[j] == 0:
            end = j
            break
    header_indent = None
    for j in range(jobs_idx + 1, end):
        if clean[j].strip() and not skip[j]:
            header_indent = indents[j]
            break
    if header_indent is None:
        return []
    headers: List[Tuple[int, str]] = []
    for j in range(jobs_idx + 1, end):
        if skip[j] or not clean[j].strip():
            continue
        if indents[j] == header_indent and re.match(
                r"^(?:\"[^\"]+\"|'[^']+'|[A-Za-z0-9_.-]+):\s*$", clean[j]):
            headers.append((j, _strip_quotes(clean[j][:-1].strip())[0]))
    for hi, (job_start, job_name) in enumerate(headers):
        job_end = headers[hi + 1][0] if hi + 1 < len(headers) else end
        job_items = [(k, indents[k], clean[k]) for k in range(job_start + 1, job_end)
                     if not skip[k] and clean[k].strip()]
        job = CiJob(workflow=relpath, job=job_name, line=job_start + 1)

        # runs-on (scalar, flow list, or block list)
        for idx, (k, ind, s) in enumerate(job_items):
            m = re.match(r"^runs-on:\s*(.*)$", s)
            if not m:
                continue
            value = m.group(1).strip()
            if value:
                if value.startswith("[") or value.startswith("{"):
                    full, _ = _collect_flow(job_items, idx, value)
                    if full.startswith("["):
                        job.runs_on.extend(_flow_list(full))
                    else:
                        labels = _flow_map(full).get("labels", "")
                        if labels.startswith("["):
                            job.runs_on.extend(_flow_list(labels))
                        elif labels and "${{" not in labels:
                            job.runs_on.append(_strip_quotes(labels)[0])
                elif "${{" not in value:
                    job.runs_on.append(_strip_quotes(value)[0])
            else:
                j = idx + 1
                if j < len(job_items) and job_items[j][1] > ind and \
                        (job_items[j][2].startswith("[") or job_items[j][2].startswith("{")):
                    full, _ = _collect_flow(job_items, j, job_items[j][2])
                    if full.startswith("["):
                        job.runs_on.extend(_flow_list(full))
                    else:
                        labels = _flow_map(full).get("labels", "")
                        if labels.startswith("["):
                            job.runs_on.extend(_flow_list(labels))
                        elif labels and "${{" not in labels:
                            job.runs_on.append(_strip_quotes(labels)[0])
                else:
                    while j < len(job_items):
                        _, ind2, s2 = job_items[j]
                        if ind2 < ind:
                            break
                        if ind2 == ind and not s2.startswith("-"):
                            break
                        if s2.startswith("-"):
                            runner = _strip_quotes(s2[1:].strip())[0]
                            if runner:
                                job.runs_on.append(runner)
                        j += 1
            break

        # matrix
        matrix_line = None
        matrix_indent = None
        matrix_rest = ""
        for idx, (k, ind, s) in enumerate(job_items):
            m = re.match(r"^matrix:\s*(.*)$", s)
            if m:
                matrix_line = k
                matrix_indent = ind
                matrix_rest = m.group(1).strip()
                break
        if matrix_line is not None:
            if matrix_rest.startswith("{"):
                _apply_flow_matrix(job, matrix_rest, cap)
            elif matrix_rest and not matrix_rest.startswith("&"):
                job.dynamic.append("matrix: %s" % matrix_rest)
            else:
                if matrix_rest.startswith("&"):
                    job.notes.append("matrix anchor; aliases in other jobs are not resolved")
                stop = len(lines)
                for k in range(matrix_line + 1, len(lines)):
                    if skip[k] or not clean[k].strip():
                        continue
                    if indents[k] <= matrix_indent:
                        stop = k
                        break
                items = [(k, indents[k], clean[k]) for k in range(matrix_line + 1, stop)
                         if not skip[k] and clean[k].strip()]
                if items and items[0][2].startswith("{"):
                    full, _ = _collect_flow(items, 0, items[0][2])
                    _apply_flow_matrix(job, full, cap)
                else:
                    parsed = _parse_matrix(items, 0, matrix_indent, job.notes, job.dynamic)
                    job.axes = parsed["axes"]
                    for v in job.axes.values():
                        if len(v) != len(set(v)):
                            job.notes.append("duplicate matrix values: %s" % v)
                    job.combos, job.truncated = _expand_matrix(
                        job.axes, parsed["include"], parsed["exclude"], cap)
                    if job.truncated:
                        job.notes.append("matrix product exceeded --max-combos; coverage approximated")

        jobs.append(job)

    # Scalar setup-* versions are coverage too (single-version workflows).
    joined = "\n".join(clean)
    scalar_specs = [
        ("actions/setup-python", r"python-version:\s*(.+)$", "python"),
        ("actions/setup-node", r"node-version:\s*(.+)$", "node"),
        ("actions/setup-go", r"go-version:\s*(.+)$", "go"),
        ("actions-rs/toolchain", r"toolchain:\s*(.+)$", "rust"),
        ("dtolnay/rust-toolchain", r"toolchain:\s*(.+)$", "rust"),
    ]
    for action, pattern, eco in scalar_specs:
        if action not in joined:
            continue
        for i, s in enumerate(clean):
            if skip[i] or not _under_key(clean, indents, i, "with"):
                continue
            if not _step_uses_action(clean, indents, i, action):
                continue
            m = re.search(pattern, s)
            if not m:
                continue
            value = m.group(1).strip()
            key = {"python": "python-version", "node": "node-version",
                   "go": "go-version", "rust": "toolchain"}[eco]
            if "${{" in value or value.startswith("["):
                if "matrix." in value.replace(" ", ""):
                    continue
                jobs.append(CiJob(workflow=relpath, job="<scalar:%s>" % eco, line=i + 1,
                                  dynamic=["%s: %s" % (key, value)]))
                continue
            value, quoted = _strip_quotes(value)
            if _float_risk(value, quoted, eco):
                jobs.append(CiJob(workflow=relpath, job="<scalar:%s>" % eco, line=i + 1,
                                  dynamic=["%s: %s" % (key, value)],
                                  notes=["unquoted %r may be read as a float by YAML; quote it" % value]))
                continue
            jobs.append(CiJob(workflow=relpath, job="<scalar:%s>" % eco, line=i + 1,
                              axes={key: [value]}, combos=[{key: value}]))

    # Flow-style `with: {python-version: "3.10"}`.
    for i, s in enumerate(clean):
        if skip[i] or not s.startswith("with:") or "{" not in s:
            continue
        actions = _step_actions(clean, indents, i)
        if not actions:
            continue
        params = _flow_map(s.split(":", 1)[1])
        for action, pattern, eco in scalar_specs:
            if not any(action in ref for ref in actions):
                continue
            key = {"python": "python-version", "node": "node-version",
                   "go": "go-version", "rust": "toolchain"}[eco]
            value = params.get(key)
            if value and "${{" not in value:
                jobs.append(CiJob(workflow=relpath, job="<scalar:%s>" % eco, line=i + 1,
                                  axes={key: [value]}, combos=[{key: value}]))

    # Action refs that encode the version, e.g. dtolnay/rust-toolchain@1.70.0.
    for i, s in enumerate(clean):
        m = re.match(r"^-?\s*uses:\s*dtolnay/rust-toolchain@(\S+)$", s)
        if m:
            ref = m.group(1)
            if normalize_rust(ref):
                jobs.append(CiJob(workflow=relpath, job="<scalar:rust>", line=i + 1,
                                  axes={"toolchain": [ref]}, combos=[{"toolchain": ref}]))
            elif ref in ("stable", "beta", "nightly"):
                jobs.append(CiJob(workflow=relpath, job="<scalar:rust>", line=i + 1,
                                  dynamic=["toolchain: %s" % ref]))

    # `*-version-file:` delegates the version to a file; statically unknown.
    for i, s in enumerate(clean):
        if skip[i] or not _under_key(clean, indents, i, "with"):
            continue
        m = re.match(r"^(python|node|go)-version-file:\s*(.+)$", s)
        if m:
            jobs.append(CiJob(workflow=relpath, job="<file:%s>" % m.group(1),
                              line=i + 1, dynamic=["%s-version-file: %s"
                                                   % (m.group(1), m.group(2))]))

    # Symbolic values (3.x, lts/*, stable, self-hosted) are not concrete:
    # mark the job dynamic so missing-version findings are not fabricated.
    for job in jobs:
        for key, values in job.axes.items():
            family = _family_of(key)
            if not family:
                continue
            for value in values:
                if not value:
                    continue
                if family == "os":
                    if _os_from_value(value) is None and not value.startswith("${{"):
                        job.dynamic.append("%s=%r is not a concrete runner" % (key, value))
                else:
                    if _normalizer(family)(value) is None:
                        job.dynamic.append("%s=%r is not a concrete version" % (key, value))
    return jobs


# ---------------------------------------------------------------------------
# analysis


def _normalizer(eco: str):
    return {"python": normalize_python, "node": normalize_node,
            "go": normalize_go, "rust": normalize_rust}[eco]


def _record_coverage(out: Dict[str, Dict[str, List[str]]], eco: str,
                     value: str, place: str) -> None:
    bucket = out.setdefault(eco, {}).setdefault(value, [])
    if place not in bucket:
        bucket.append(place)


def _ecosystem_coverage(report: Report) -> Dict[str, Dict[str, List[str]]]:
    out: Dict[str, Dict[str, List[str]]] = {}
    for job in report.jobs:
        cov = job.coverage()
        for key, values in cov.items():
            eco = _family_of(key)
            if not eco:
                continue
            norm = _normalizer(eco) if eco != "os" else (lambda x: x)
            place = "%s:%s" % (job.workflow, job.job)
            for value in values:
                if eco == "os":
                    mapped = _os_from_value(value)
                    if mapped:
                        _record_coverage(out, "os", mapped, place)
                else:
                    nv = norm(value)
                    if nv:
                        _record_coverage(out, eco, nv, place)
        for runner in job.runs_on:
            mapped = _os_from_value(runner)
            if mapped:
                _record_coverage(out, "os", mapped, "%s:%s" % (job.workflow, job.job))
    return out


def _os_from_value(value: str) -> Optional[str]:
    v = value.strip().lower()
    if v.startswith("${{"):
        return None
    if "ubuntu" in v or "linux" in v:
        return "linux"
    if "windows" in v:
        return "windows"
    if "macos" in v or v.startswith("mac"):
        return "macos"
    return None


def _dynamic_ecosystems(jobs: Sequence[CiJob]) -> Set[str]:
    """Map dynamic reasons to the ecosystem they make unknowable."""
    out: Set[str] = set()
    for job in jobs:
        for reason in job.dynamic:
            lowered = reason.lower()
            if lowered.startswith("matrix:") or "flow-style" in lowered:
                out.add("*")
                continue
            head = reason.split(":", 1)[0].split("=", 1)[0].strip().lower()
            if head.endswith("-version-file"):
                head = head[:-len("-version-file")] + "-version"
            family = _family_of(head)
            if family:
                out.add(family)
            else:
                out.add("*")
    return out


def _claimed_by_eco(report: Report) -> Dict[str, List[Claim]]:
    out: Dict[str, List[Claim]] = {}
    for claim in report.claims:
        out.setdefault(claim.ecosystem, []).append(claim)
    return out


def _severity_for(fail_on: str) -> int:
    return SEVERITY_RANK[fail_on]


def analyze(root: Path, max_combos: int = 5000, use_eol: bool = False,
            today: str = "", eol_file: Optional[Path] = None) -> Report:
    report = Report(root=str(root))
    if not root.exists():
        raise ToolError("path does not exist: %s" % root)
    if not root.is_dir():
        raise ToolError("not a directory: %s" % root)

    files = {
        "pyproject.toml": parse_pyproject,
        "setup.cfg": parse_setup_cfg,
        "setup.py": parse_setup_py,
        "package.json": parse_package_json,
        "Cargo.toml": parse_cargo_toml,
        "go.mod": parse_go_mod,
        "tox.ini": parse_tox,
    }
    for name, parser in files.items():
        p = root / name
        if p.is_file():
            try:
                parser(p, root, report)
            except Exception as exc:
                report.warnings.append("%s: %s" % (name, exc))

    workflow_dir = root / ".github" / "workflows"
    if workflow_dir.is_dir():
        try:
            workflow_files = sorted(p for p in workflow_dir.iterdir()
                                    if p.is_file() and p.suffix.lower() in (".yml", ".yaml"))
        except OSError as exc:
            report.warnings.append("%s: could not list workflows (%s)"
                                   % (rel(workflow_dir, root), exc))
            workflow_files = []
        for p in workflow_files:
            try:
                report.jobs.extend(parse_workflow(p, root, report, max_combos))
            except Exception as exc:
                report.warnings.append("%s: %s" % (rel(p, root), exc))
    report.coverage = _ecosystem_coverage(report)

    claimed = _claimed_by_eco(report)
    findings: List[Finding] = []

    # Python: requires-python and classifiers must agree; every claimed version
    # in the released set should be tested.
    py_claims = claimed.get("python", [])
    py_versions: Set[str] = set()
    py_spec_sets: List[Tuple[Claim, Set[str]]] = []
    for c in py_claims:
        py_versions.update(c.versions)
        if c.kind == "requires-python":
            py_spec_sets.append((c, set(c.versions)))
    for c in py_claims:
        if c.kind == "classifier":
            for spec_claim, spec_set in py_spec_sets:
                if c.versions:
                    allowed = _spec_allows(spec_claim.value, c.versions[0])
                    if allowed is False:
                        findings.append(Finding(
                            "MG002", "high", "python",
                            "classifier claims Python %s but %s excludes it"
                            % (c.versions[0], spec_claim.value),
                            detail="%s:%d vs %s:%d" % (c.source, c.line, spec_claim.source, spec_claim.line),
                            source=c.source, line=c.line))
    py_cov = set(report.coverage.get("python", {}))
    dynamic = _dynamic_ecosystems(report.jobs)
    dynamic_any = "*" in dynamic
    dynamic_py = dynamic_any or "python" in dynamic
    if py_versions and not py_cov and not dynamic_py:
        findings.append(Finding(
            "MG009", "high", "python",
            "claims support for %s but no workflow tests any Python version"
            % _fmt_versions(sorted(py_versions, key=_py_sort)),
            detail=_claim_sources(py_claims)))
    elif py_versions and py_cov:
        for v in sorted(py_versions - py_cov, key=_py_sort):
            if dynamic_py:
                continue
            findings.append(Finding(
                "MG001", "high", "python",
                "Python %s is claimed but not tested" % v,
                detail="claimed by %s" % _claim_sources([c for c in py_claims if v in c.versions]),
                source=py_claims[0].source if py_claims else "", line=py_claims[0].line if py_claims else 0))
        for v in sorted(py_cov - py_versions, key=_py_sort):
            if py_versions:
                findings.append(Finding(
                    "MG003", "medium", "python",
                    "CI tests Python %s, which is outside the claimed range" % v,
                    detail=_coverage_source(report, "python", v)))

    # Node
    node_claims = claimed.get("node", [])
    node_versions: Set[str] = set()
    for c in node_claims:
        node_versions.update(c.versions)
    node_cov = set(report.coverage.get("node", {}))
    dynamic_node = dynamic_any or "node" in dynamic
    if node_versions and not node_cov and not dynamic_node:
        findings.append(Finding(
            "MG009", "high", "node",
            "claims support for Node %s but no workflow tests any Node version"
            % _fmt_versions(sorted(node_versions, key=int)),
            detail=_claim_sources(node_claims)))
    elif node_versions and node_cov:
        for v in sorted({v for v in node_versions if v in NODE_LTS} - node_cov, key=int):
            findings.append(Finding(
                "MG001", "high", "node",
                "Node %s is claimed but not tested" % v,
                detail="claimed by %s" % _claim_sources([c for c in node_claims if v in c.versions])))
        for v in sorted(node_cov - node_versions, key=int):
            findings.append(Finding(
                "MG003", "medium", "node",
                "CI tests Node %s, which is outside the claimed range" % v,
                detail=_coverage_source(report, "node", v)))

    # Go: the go directive is a minimum language version. Check that the
    # minimum is actually tested; newer versions are forward compatible.
    go_claims = claimed.get("go", [])
    go_cov = set(report.coverage.get("go", {}))
    dynamic_go = dynamic_any or "go" in dynamic
    if go_claims and not go_cov and not dynamic_go:
        findings.append(Finding("MG009", "high", "go",
                                "declares a Go language version but no workflow tests Go",
                                detail=_claim_sources(go_claims)))
    elif go_claims and go_cov and not dynamic_go:
        minimum = min((c.versions[0] for c in go_claims if c.versions), key=_go_sort)
        below = [v for v in go_cov if _go_sort(v) < _go_sort(minimum)]
        if not any(_go_sort(v) >= _go_sort(minimum) for v in go_cov):
            findings.append(Finding("MG001", "high", "go",
                                    "go directive requires %s but CI tests only %s"
                                    % (minimum, ", ".join(sorted(go_cov, key=_go_sort))),
                                    detail=_claim_sources(go_claims)))
        else:
            for v in sorted(below, key=_go_sort):
                findings.append(Finding("MG003", "medium", "go",
                                        "CI tests Go %s below the declared minimum %s" % (v, minimum),
                                        detail=_coverage_source(report, "go", v)))

    # Rust: MSRV must be tested explicitly.
    rust_claims = claimed.get("rust", [])
    rust_cov = set(report.coverage.get("rust", {}))
    unknown_rust = any(
        any(k.lower() in RUST_KEYS for k in job.axes)
        and any(("fromJSON" in reason or reason.startswith("matrix:")
                 or "anchor" in reason or "version-file" in reason)
                for reason in job.dynamic)
        for job in report.jobs)
    if rust_claims and not unknown_rust:
        msrv = min((c.versions[0] for c in rust_claims if c.versions), key=_rust_sort)
        if msrv not in rust_cov:
            findings.append(Finding("MG011", "high", "rust",
                                    "MSRV %s is declared but not tested (CI: %s)"
                                    % (msrv, ", ".join(sorted(rust_cov)) if rust_cov else "none"),
                                    detail=_claim_sources(rust_claims)))

    # Operating systems
    os_claims = claimed.get("os", [])
    os_claimed = {c.value for c in os_claims}
    os_cov = set(report.coverage.get("os", {}))
    dynamic_os = dynamic_any or "os" in dynamic
    if os_claimed and not os_cov and not dynamic_os:
        findings.append(Finding("MG009", "high", "os",
                                "claims %s support but no workflow has a concrete runner"
                                % ", ".join(sorted(os_claimed)),
                                detail=_claim_sources(os_claims)))
    elif os_claimed and not dynamic_os:
        for os_name in sorted(os_claimed - os_cov):
            findings.append(Finding("MG004", "medium", "os",
                                    "%s is claimed but no CI job runs there" % os_name,
                                    detail=_claim_sources([c for c in os_claims if c.value == os_name])))

    # Dynamic / unparseable matrix
    for job in report.jobs:
        if job.dynamic:
            findings.append(Finding(
                "MG005", "low", job_dynamic_eco(job),
                "matrix expression not expanded; coverage may be incomplete",
                detail="%s:%d %s" % (job.workflow, job.line, "; ".join(job.dynamic))))
        if job.truncated:
            findings.append(Finding(
                "MG010", "low", "matrix",
                "matrix has more than --max-combos combinations; coverage approximated",
                detail="%s:%d %s" % (job.workflow, job.line, job.job)))
        for note in job.notes:
            if "float" in note:
                findings.append(Finding(
                    "MG006", "low", "matrix",
                    "unquoted version may be parsed as a float",
                    detail="%s:%d %s" % (job.workflow, job.line, note)))
            elif "duplicate" in note:
                findings.append(Finding(
                    "MG007", "low", "matrix",
                    "duplicate values in matrix",
                    detail="%s:%d %s: %s" % (job.workflow, job.line, job.job, note)))

    # EOL (opt-in)
    if use_eol:
        table = {"python": dict(EOL_PYTHON), "node": dict(EOL_NODE)}
        if eol_file is not None:
            if not eol_file.is_file():
                raise ToolError("EOL file not found: %s" % eol_file)
            try:
                extra = json.loads(read_text(eol_file))
            except Exception as exc:
                raise ToolError("could not read EOL file %s: %s" % (eol_file, exc))
            if not isinstance(extra, dict):
                raise ToolError("EOL file must contain a JSON object")
            for eco in ("python", "node"):
                if eco not in extra:
                    continue
                if not isinstance(extra[eco], dict):
                    raise ToolError("EOL file entry %r must be an object" % eco)
                for key, value in extra[eco].items():
                    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", value) \
                            or not _valid_date(value):
                        raise ToolError("EOL date for %s %s must be a real YYYY-MM-DD date"
                                        % (eco, key))
                    table[eco][str(key)] = value
        today_s = today or _today()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", today_s) or not _valid_date(today_s):
            raise ToolError("--today must be a real YYYY-MM-DD date")
        for eco, versions in (("python", py_versions), ("node", node_versions)):
            for v in sorted(versions):
                eol = table.get(eco, {}).get(v)
                if eol and eol <= today_s:
                    findings.append(Finding(
                        "MG008", "medium", eco,
                        "%s %s is past end of life (%s)" % (eco, v, eol),
                        detail="claim still advertises an EOL version"))

    unique: List[Finding] = []
    seen_findings: Set[Tuple[str, str]] = set()
    for f in findings:
        key = (f.code, f.message)
        if key not in seen_findings:
            seen_findings.add(key)
            unique.append(f)
    report.findings = unique
    return report


def job_dynamic_eco(job: CiJob) -> str:
    for key in job.axes:
        eco = _family_of(key)
        if eco:
            return eco
    return "matrix"


def _py_sort(v: str) -> Tuple[int, int]:
    return _py_tuple(v)[:2]  # type: ignore


def _go_sort(v: str) -> Tuple[int, int]:
    m = re.match(r"^(\d+)\.(\d+)", v)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _rust_sort(v: str) -> Tuple[int, int]:
    return _go_sort(v)


def _fmt_versions(values: Sequence[str], limit: int = 6) -> str:
    if len(values) <= limit:
        return ", ".join(values)
    return ", ".join(values[:limit]) + ", ..."


def _claim_sources(claims: Sequence[Claim]) -> str:
    seen: List[str] = []
    for c in claims:
        s = "%s:%d" % (c.source, c.line)
        if s not in seen:
            seen.append(s)
    return ", ".join(seen) if seen else "unknown"


def _coverage_source(report: Report, eco: str, value: str) -> str:
    places = report.coverage.get(eco, {}).get(value, [])
    return ", ".join(places) if places else "unknown"


def _valid_date(value: str) -> bool:
    import datetime
    try:
        datetime.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------------------
# rendering


def _sev_color(sev: str, text: str, enabled: bool) -> str:
    if not enabled:
        return text
    code = {"high": "31", "medium": "33", "low": "36"}.get(sev, "0")
    return "\x1b[%sm%s\x1b[0m" % (code, text)


def render_human(report: Report, min_severity: str, fail_on: str) -> str:
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    out: List[str] = []
    out.append("matrix-gap %s -- %s" % (__version__, report.root))
    out.append("")
    out.append("CLAIMS")
    if report.claims:
        for c in report.claims:
            out.append("  %-7s %-38s %s:%d" % (c.ecosystem, _clip(c.value, 38), c.source, c.line))
    else:
        out.append("  (none found)")
    out.append("")
    out.append("CI COVERAGE")
    if report.coverage:
        for eco in sorted(report.coverage):
            covered = ", ".join(sorted(report.coverage[eco], key=_eco_sort(eco)))
            first = sorted(report.coverage[eco], key=_eco_sort(eco))[0]
            out.append("  %-7s %-38s %s" % (eco, _clip(covered, 38),
                                            ", ".join(report.coverage[eco][first])))
    else:
        out.append("  (no concrete coverage found)")
    out.append("")
    shown = [f for f in report.findings if SEVERITY_RANK[f.severity] >= SEVERITY_RANK[min_severity]]
    out.append("FINDINGS")
    if shown:
        for f in shown:
            label = _sev_color(f.severity, f.severity.upper().ljust(6), color)
            out.append("  %s %-6s %s" % (label, f.code, f.message))
            if f.detail:
                out.append("         %s" % f.detail)
    else:
        out.append("  none")
    if report.warnings:
        out.append("")
        out.append("NOTES")
        for w in report.warnings:
            out.append("  - %s" % w)
    counts = {s: sum(1 for f in report.findings if f.severity == s) for s in ("high", "medium", "low")}
    out.append("")
    out.append("%d high, %d medium, %d low" % (counts["high"], counts["medium"], counts["low"]))
    if fail_on != "none":
        out.append("fail threshold: %s" % fail_on)
    return "\n".join(out)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _eco_sort(eco: str):
    if eco in ("python", "go", "rust"):
        return lambda v: _go_sort(v) if eco != "python" else _py_sort(v)
    if eco == "node":
        return lambda v: int(v) if v.isdigit() else 0
    return lambda v: v


def render_json(report: Report) -> str:
    payload = {
        "tool": "matrix-gap",
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "root": report.root,
        "claims": [asdict(c) for c in report.claims],
        "ci": {
            "jobs": [
                {
                    "workflow": j.workflow,
                    "job": j.job,
                    "line": j.line,
                    "axes": j.axes,
                    "runs_on": j.runs_on,
                    "dynamic": j.dynamic,
                    "truncated": j.truncated,
                    "notes": j.notes,
                }
                for j in report.jobs
            ],
            "coverage": {eco: {v: places for v, places in sorted(vals.items())}
                         for eco, vals in report.coverage.items()},
        },
        "findings": [asdict(f) for f in report.findings],
        "warnings": report.warnings,
        "summary": {
            "high": sum(1 for f in report.findings if f.severity == "high"),
            "medium": sum(1 for f in report.findings if f.severity == "medium"),
            "low": sum(1 for f in report.findings if f.severity == "low"),
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=True)


# ---------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="matrix-gap",
        description="Audit claimed platform support against the CI matrix that actually tests it.",
    )
    p.add_argument("path", nargs="?", default=".", help="repository root (default: .)")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.add_argument("--min-severity", choices=("low", "medium", "high"), default="low",
                   help="only show findings at or above this severity (default: low)")
    p.add_argument("--fail-on", choices=("none", "low", "medium", "high"), default="high",
                   help="exit 1 when a finding at or above this severity exists (default: high)")
    p.add_argument("--max-combos", type=int, default=5000,
                   help="cap on expanded matrix combinations (default: 5000)")
    p.add_argument("--eol", action="store_true", help="also report end-of-life versions (bundled snapshot)")
    p.add_argument("--eol-file", type=Path, default=None,
                   help="JSON file with extra EOL dates, e.g. {\"python\": {\"3.9\": \"2025-10-31\"}}")
    p.add_argument("--today", default="", help="override today's date (YYYY-MM-DD) for EOL checks")
    p.add_argument("--version", action="version", version="matrix-gap " + __version__)
    return p


def _emit(text: str) -> None:
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        sys.stdout.write(text.encode(enc, "backslashreplace").decode(enc) + "\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_combos < 1:
        print("matrix-gap: error: --max-combos must be >= 1", file=sys.stderr)
        return 2
    if args.today and (not re.match(r"^\d{4}-\d{2}-\d{2}$", args.today)
                       or not _valid_date(args.today)):
        print("matrix-gap: error: --today must be a real YYYY-MM-DD date", file=sys.stderr)
        return 2
    try:
        report = analyze(Path(args.path), max_combos=args.max_combos,
                         use_eol=args.eol or args.eol_file is not None,
                         today=args.today, eol_file=args.eol_file)
    except ToolError as exc:
        print("matrix-gap: error: %s" % exc, file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        print("matrix-gap: internal error: %s" % exc, file=sys.stderr)
        return 2

    if args.json:
        _emit(render_json(report))
    else:
        _emit(render_human(report, args.min_severity, args.fail_on))

    if args.fail_on == "none":
        return 0
    threshold = SEVERITY_RANK[args.fail_on]
    return 1 if any(SEVERITY_RANK[f.severity] >= threshold for f in report.findings) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os._exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
