# -*- coding: utf-8 -*-
"""Minimal evaluator for vendored public checks.

DELIBERATELY STANDALONE. This module must never import `odexalabs`: the
engine is private, and anything shipped to the App Store is readable by
anyone who installs it (see the repo's CLAUDE.md). It reads
`data/public_checks.json`, which the engine exports with
`distribution: public` checks only, and evaluates their `condition`
strings against a row of facts this module gathered itself.

It is not a reimplementation of the engine. It supports exactly the
subset of expression syntax the vendored checks use, and refuses anything
else rather than guessing.

Three-state result, matching the engine's own semantics:

    fired    the condition evaluated True
    clear    the condition evaluated False
    skipped  the condition could not be trusted or could not be evaluated

`skipped` is not a soft `clear`, and the distinction is the whole point.
A migration check that reports "no problem" on a run where nobody stated
a target has told the reader something false. Any field listed in
`untrusted_fields` forces `skipped` for every condition that reads it.
"""

import ast
import datetime
import json
import os

FIRED = "fired"
CLEAR = "clear"
SKIPPED = "skipped"

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


class ConditionError(ValueError):
    """The condition uses syntax this evaluator deliberately does not support."""


def load_public_checks(path=None):
    """Load the vendored public check definitions."""
    path = path or os.path.join(_DATA_DIR, "public_checks.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_native_checks(path=None):
    """Load the module-native check definitions (MRS-*).

    Same shape as the vendored public checks, so one evaluator runs both and
    one test can read every shipped condition. These are computed by this
    module and are in no engine registry, which is why they carry `MRS-*`
    ids and never `MIG-*`.
    """
    path = path or os.path.join(_DATA_DIR, "native_checks.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_support_matrix(path=None):
    """Load the vendored Odoo version support facts.

    The file deliberately carries no `status`: a stored status goes stale
    the day a support date passes. `support_status()` derives it from the
    dates every time a scan runs.
    """
    path = path or os.path.join(_DATA_DIR, "support_matrix.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


ENDING_SOON_MONTHS = 6

_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)


def _parse_date(value):
    return datetime.date(*(int(part) for part in str(value).split("-")[:3]))


def _month_index(date):
    return date.year * 12 + (date.month - 1)


def support_end_month(entry):
    """The published end-of-support month as text, e.g. "September 2026"."""
    if not entry or not entry.get("support_ends"):
        return None
    try:
        ends = _parse_date(entry["support_ends"])
    except (TypeError, ValueError):
        return None
    return "%s %d" % (_MONTHS[ends.month - 1], ends.year)


def support_status(entry, today=None):
    """"supported" / "ending_soon" / "unsupported", computed from the dates.

    unsupported  from the first day of the month after the published end month
    ending_soon  from ENDING_SOON_MONTHS before the end month through that month
    supported    otherwise

    Returns None when the entry has no usable end date, never a guess.
    """
    if not entry or not entry.get("support_ends"):
        return None
    try:
        ends = _parse_date(entry["support_ends"])
    except (TypeError, ValueError):
        return None
    now = _month_index(today or datetime.date.today())
    end = _month_index(ends)
    if now > end:
        return "unsupported"
    if now >= end - ENDING_SOON_MONTHS:
        return "ending_soon"
    return "supported"


def load_standard_modules(path=None):
    """{series: {module_name: licence}} for the series vendored with the module."""
    path = path or os.path.join(_DATA_DIR, "standard_modules.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def matrix_entry(version, matrix=None):
    """The support-matrix row for `version`, or None if it is not listed."""
    for entry in matrix if matrix is not None else load_support_matrix():
        if entry["version"] == version:
            return entry
    return None


def normalize_version(value, matrix=None):
    """Resolve "19" or "19.0" to the matrix's canonical "19.0".

    Returns None when the value is unknown or ambiguous. Never guesses:
    two spellings of one target must not become two different results.
    """
    if not value:
        return None
    value = value.strip()
    known = [e["version"] for e in (matrix if matrix is not None else load_support_matrix())]
    if value in known:
        return value
    candidates = [v for v in known if v.startswith(value + ".")]
    return candidates[0] if len(candidates) == 1 else None


_COMPARE = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: None if (a is None or b is None) else a < b,
    ast.LtE: lambda a, b: None if (a is None or b is None) else a <= b,
    ast.Gt: lambda a, b: None if (a is None or b is None) else a > b,
    ast.GtE: lambda a, b: None if (a is None or b is None) else a >= b,
}


def _names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _eval(node, row):
    if isinstance(node, ast.Expression):
        return _eval(node.body, row)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "None":
            return None
        if node.id not in row:
            raise ConditionError("unknown field %r" % node.id)
        return row[node.id]
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            for value in node.values:
                if _eval(value, row) is not True:
                    return False
            return True
        if isinstance(node.op, ast.Or):
            for value in node.values:
                if _eval(value, row) is True:
                    return True
            return False
        raise ConditionError("unsupported boolean operator")
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _eval(node.operand, row)
        if isinstance(node.op, ast.USub):
            operand = _eval(node.operand, row)
            return None if operand is None else -operand
        raise ConditionError("unsupported unary operator")
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise ConditionError("chained comparisons are not supported")
        op = _COMPARE.get(type(node.ops[0]))
        if op is None:
            raise ConditionError("unsupported comparison operator")
        return op(_eval(node.left, row), _eval(node.comparators[0], row))
    raise ConditionError("unsupported syntax (%s)" % type(node).__name__)


def evaluate(condition, row, untrusted_fields=()):
    """Evaluate `condition` against `row`.

    Returns one of FIRED / CLEAR / SKIPPED, plus a reason for SKIPPED.

    `untrusted_fields` is a list of field names, or a dict mapping a field
    name to the reason it could not be established. Either way, a condition
    reading one of them is SKIPPED, never evaluated against a placeholder.
    """
    try:
        tree = ast.parse(condition.strip(), mode="eval")
    except SyntaxError as exc:
        return SKIPPED, "condition could not be parsed: %s" % exc

    reasons = untrusted_fields if isinstance(untrusted_fields, dict) else {}
    referenced = _names(tree)
    blocked = sorted(referenced & set(untrusted_fields or ()))
    if blocked:
        explained = [reasons[name] for name in blocked if reasons.get(name)]
        if explained:
            return SKIPPED, "; ".join(explained)
        return SKIPPED, (
            "reads %s, which this scan could not establish" % ", ".join(blocked)
        )

    try:
        result = _eval(tree, row)
    except ConditionError as exc:
        return SKIPPED, str(exc)

    if result is True:
        return FIRED, None
    return CLEAR, None


def run_checks(row, untrusted_fields=(), checks=None):
    """Evaluate every vendored check against one row of facts."""
    results = []
    for check in checks if checks is not None else load_public_checks():
        status, reason = evaluate(check["condition"], row, untrusted_fields)
        results.append(
            {
                "id": check["id"],
                "name": check["name"],
                "severity": check.get("severity") or False,
                "status": status,
                "reason": reason,
                "symptom": check.get("symptom", ""),
                "business_impact": check.get("business_impact", ""),
                "technical_explanation": check.get("technical_explanation", ""),
                "recommendation": check.get("recommendation", ""),
                "handbook": check.get("handbook", ""),
                "evidence": {k: row.get(k) for k in check.get("evidence", []) if k in row},
            }
        )
    return results
