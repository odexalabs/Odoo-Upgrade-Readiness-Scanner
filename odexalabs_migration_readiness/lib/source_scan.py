# -*- coding: utf-8 -*-
"""Read-only file access for INV-001 and INV-006.

Reads module directories on the addons path: their size, the raw manifest,
the README, the addons checkout's `.git/config`, and the Python/XML source
for deprecated-code patterns. Opens files for reading only; writes nothing;
fetches nothing.

INV-006 is PATTERN MATCHING, line by line. It produces false positives and
cannot prove that code is clean. The UI says so beside the results.
"""

import ast
import fnmatch
import json
import os
import re

from .origin import strip_credentials

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".tox", ".venv"}
MAX_SCAN_BYTES = 2 * 1024 * 1024
_REMOTE_URL = re.compile(r"^\s*url\s*=\s*(\S+)\s*$")


def footprint(path):
    """(bytes, files) under a module directory, skipping caches and VCS data."""
    total = files = 0
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in names:
            if fname.endswith(".pyc"):
                continue
            try:
                total += os.path.getsize(os.path.join(root, fname))
                files += 1
            except OSError:
                continue
    return total, files


def read_raw_manifest(path):
    """The manifest as written on disk (literal dict), or None.

    Read raw, not through Odoo's loader, so that a `version` naming an older
    series is seen as written rather than after Odoo rewrites it.
    """
    for fname in ("__manifest__.py", "__openerp__.py"):
        manifest = os.path.join(path, fname)
        if os.path.isfile(manifest):
            try:
                with open(manifest, encoding="utf-8") as handle:
                    value = ast.literal_eval(handle.read())
                return value if isinstance(value, dict) else None
            except (OSError, ValueError, SyntaxError, UnicodeDecodeError):
                return None
    return None


def read_readme(path, limit=200000):
    for fname in ("README.rst", "README.md", "README.txt", "README"):
        readme = os.path.join(path, fname)
        if os.path.isfile(readme):
            try:
                with open(readme, encoding="utf-8", errors="replace") as handle:
                    return handle.read(limit)
            except OSError:
                return None
    return None


def has_store_packaging(path, manifest):
    if manifest and manifest.get("images"):
        return True
    return os.path.isfile(os.path.join(path, "static", "description", "index.html"))


def _git_config_for(git_path):
    """The config file of a repository whose .git is `git_path` (dir or file)."""
    if os.path.isdir(git_path):
        return os.path.join(git_path, "config")
    try:
        with open(git_path, encoding="utf-8") as handle:
            line = handle.readline().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gitdir = line.split(":", 1)[1].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.normpath(os.path.join(os.path.dirname(git_path), gitdir))
    commondir = os.path.join(gitdir, "commondir")
    if os.path.isfile(commondir):
        try:
            with open(commondir, encoding="utf-8") as handle:
                rel = handle.read().strip()
            gitdir = os.path.normpath(os.path.join(gitdir, rel))
        except OSError:
            pass
    return os.path.join(gitdir, "config")


def git_remotes(path):
    """Remote URLs of the checkout holding this module: the module directory
    itself or its parent (the addons checkout). Credentials are stripped.
    Looks no further up, so a large repository far above the addons path is
    never mistaken for the module's origin.
    """
    for candidate in (path, os.path.dirname(path)):
        git_path = os.path.join(candidate, ".git")
        if not os.path.exists(git_path):
            continue
        config = _git_config_for(git_path)
        if not config or not os.path.isfile(config):
            return []
        urls = []
        try:
            with open(config, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    match = _REMOTE_URL.match(line)
                    if match:
                        url = strip_credentials(match.group(1))
                        if url not in urls:
                            urls.append(url)
        except OSError:
            return []
        return urls
    return []


def load_patterns(path=None):
    path = path or os.path.join(_DATA_DIR, "deprecation_patterns.json")
    with open(path, encoding="utf-8") as handle:
        patterns = json.load(handle)
    for pattern in patterns:
        pattern["_re"] = re.compile(pattern["regex"])
        pattern["_skip"] = re.compile(pattern["skip_path"]) if pattern.get("skip_path") else None
    return patterns


def _series_key(series):
    try:
        major, minor = str(series).split(".")[:2]
        return int(major), int(minor)
    except (ValueError, AttributeError):
        return None


def _events(pattern):
    """The series at which a pattern's code changes: deprecated/rewritten
    (`since`) and, where catalogued, removed later (`removed`)."""
    return [k for k in (_series_key(pattern.get("since")), _series_key(pattern.get("removed"))) if k]


def relevant_patterns(patterns, current, target):
    """Patterns with a change after the current series and by the target."""
    cur, tgt = _series_key(current), _series_key(target)
    if cur is None or tgt is None or tgt <= cur:
        return []
    return [p for p in patterns if any(cur < event <= tgt for event in _events(p))]


def uncatalogued_gap(patterns, current, target):
    """The series after the current one and up to the target for which the
    catalogue lists no change at all, e.g. ["21.0"].

    Returns [] when every series in that window is covered. Otherwise an empty
    INV-006 result for an uncovered series would read as clean code, which it
    is not: it means nothing was looked for. A series counts as covered when
    at least one entry has its `since` or `removed` there.
    """
    cur, tgt = _series_key(current), _series_key(target)
    if cur is None or tgt is None or tgt <= cur:
        return []
    covered = {event[0] for p in patterns for event in _events(p)}
    return ["%d.0" % major for major in range(cur[0] + 1, tgt[0] + 1) if major not in covered]


def scan_module(module, path, patterns):
    """Pattern hits in one module's source. Returns (hits, skipped_files, files_read)."""
    hits, skipped = [], []
    files_read = 0
    if not patterns:
        return hits, skipped, files_read
    for root, dirs, names in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for fname in sorted(names):
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, path).replace(os.sep, "/")
            wanted = [
                p for p in patterns
                if any(fnmatch.fnmatch(fname, g) for g in p["glob"])
                and not (p.get("_skip") and p["_skip"].search(rel))
            ]
            if not wanted:
                continue
            try:
                if os.path.getsize(full) > MAX_SCAN_BYTES:
                    skipped.append("%s/%s" % (module, rel))
                    continue
                with open(full, encoding="utf-8", errors="replace") as handle:
                    files_read += 1
                    for lineno, line in enumerate(handle, 1):
                        for pattern in wanted:
                            if pattern["_re"].search(line):
                                hits.append(
                                    {
                                        "module": module,
                                        "file": rel,
                                        "line": lineno,
                                        "pattern": pattern["id"],
                                        "since": pattern["since"] + (
                                            ", removed %s" % pattern["removed"] if pattern.get("removed") else ""
                                        ),
                                        "text": line.strip()[:240],
                                    }
                                )
            except OSError:
                skipped.append("%s/%s" % (module, rel))
    return hits, skipped, files_read
