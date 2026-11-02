#!/usr/bin/env python3
"""Check every requirement in a requirements.txt against PyPI.

Usage:
    python check_requirements.py [path/to/requirements.txt]

Needs: pip install packaging
"""
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

TARGET_PYTHON = "3.11"
PYPI_URL = "https://pypi.org/pypi/{}/json"


# --------------------------------------------------------------------------- #
# Parsing the requirements file
# --------------------------------------------------------------------------- #
def read_requirement_lines(path):
    """Return the requirement lines (comments, blanks and pip options removed)."""
    lines = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-"):  # -r, -e, -c, --index-url, ...
                print(f"skipping option line: {line}", file=sys.stderr)
                continue
            line = re.sub(r"\s+#.*$", "", line)  # inline comment
            line = re.sub(r"\s+--hash=\S+", "", line)  # hash pins
            line = line.rstrip("\\").strip()
            if line:
                lines.append(line)
    return lines


# --------------------------------------------------------------------------- #
# PyPI access
# --------------------------------------------------------------------------- #
_cache = {}


def fetch_project(name):
    """Return the PyPI JSON for a project, or None on 404."""
    key = canonicalize_name(name)
    if key in _cache:
        return _cache[key]
    req = urllib.request.Request(
        PYPI_URL.format(key), headers={"User-Agent": "requirements-checker/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            data = None
        else:
            raise
    _cache[key] = data
    return data


def release_requires_python(files):
    """requires_python of a release (taken from its files); None if unset."""
    for f in files:
        rp = f.get("requires_python")
        if rp:
            return rp
    return None


# --------------------------------------------------------------------------- #
# Python-version logic
# --------------------------------------------------------------------------- #
def allows_target(requires_python):
    if not requires_python:
        return True  # no requires_python -> counts as compatible
    try:
        return SpecifierSet(requires_python).contains(TARGET_PYTHON, prereleases=True)
    except InvalidSpecifier:
        return True  # unparsable metadata: don't flag it


def minimum_python(requires_python):
    """Lowest Python version a requires_python string allows (lower bounds only).

    '<4' or '!=3.0.*' have no lower bound -> Version('0').
    """
    try:
        spec = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return Version("0")
    lowest = Version("0")
    for s in spec:
        if s.operator not in (">=", ">", "~=", "==", "==="):
            continue
        try:
            v = Version(s.version.removesuffix(".*"))
        except InvalidVersion:
            continue
        lowest = max(lowest, v)
    return lowest


# --------------------------------------------------------------------------- #
# Checking one requirement
# --------------------------------------------------------------------------- #
def check(line):
    result = {"line": line, "status": None}
    try:
        req = Requirement(line)
    except InvalidRequirement:
        result.update(status="not_found", reason="could not parse line")
        return result
    if req.url:
        result.update(status="not_found", reason="direct URL requirement, not on PyPI")
        return result

    data = fetch_project(req.name)
    if data is None:
        result.update(status="not_found", reason="404")
        return result

    # releases that actually exist (have at least one file) and satisfy the spec
    candidates = []
    for ver_str, files in data.get("releases", {}).items():
        if not files:
            continue
        try:
            v = Version(ver_str)
        except InvalidVersion:
            continue
        if req.specifier.contains(v, prereleases=True):
            candidates.append((v, ver_str, files))

    if not candidates:
        result.update(status="not_found", reason="no matching release")
        return result

    stable = [c for c in candidates if not c[0].is_prerelease]
    v, ver_str, files = max(stable or candidates, key=lambda c: c[0])
    rp = release_requires_python(files)

    result.update(
        status="found",
        label=f"{req.name}=={ver_str}",
        requires_python=rp,
        compatible=allows_target(rp),
    )
    return result


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "requirements.txt"
    lines = read_requirement_lines(path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(check, lines))

    not_found = [r for r in results if r["status"] == "not_found"]
    incompatible = [
        r for r in results if r["status"] == "found" and not r["compatible"]
    ]
    with_rp = [r for r in results if r["status"] == "found" and r["requires_python"]]

    print(f"Dependencies checked: {len(lines)}")

    print(f"Not found on PyPI: {len(not_found)}")
    for r in not_found:
        print(f"  - {r['line']} ({r['reason']})")

    print(f"Incompatible with python {TARGET_PYTHON}: {len(incompatible)}")
    for r in incompatible:
        print(f"  - {r['label']} (requires_python: {r['requires_python']})")

    if with_rp:
        top = max(with_rp, key=lambda r: minimum_python(r["requires_python"]))
        print(
            f"highest requirement: {top['label']} "
            f"(>={minimum_python(top['requires_python'])})"
        )
    else:
        print("Highest requirement: none")


if __name__ == "__main__":
    main()